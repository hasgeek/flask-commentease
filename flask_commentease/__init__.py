# -*- coding: utf-8 -*-
"""
    flask.ext.commentease
    ~~~~~~~~~~~~~~~~~~~~~

    Flask extension for comments and votes on any database model

    :copyright: (c) 2011-12 by HasGeek Media LLP.
    :license: BSD, see LICENSE for more details.
"""

from markdown import Markdown
import bleach
from sqlalchemy import Column, ForeignKey, Boolean
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.declarative import declared_attr, synonym_for
from flask import Markup, request
import flask.ext.wtf as wtf
from coaster.sqlalchemy import TimestampMixin, BaseMixin, BaseScopedIdMixin

__all__ = ['Commentease', 'CommentingMixin', 'VotingMixin', 'CommenteaseActionError']


class VOTE_PATTERN:
    UP_ONLY = 1   # Allow only +1 votes
    UP_DOWN = 2   # Allow +1 or -1 votes (for forums)
    RANGE = 3     # Allow a voting range (scale of 1 to 10; -1, 0, +1, etc)
    CUSTOM = 4    # Custom value storage (Commentease defers to app)


class COMMENT_STATUS:
    DRAFT = 1     # Partially composed comment
    PUBLIC = 2    # Regular comment
    SCREENED = 3  # Awaiting approval
    HIDDEN = 4    # Hidden by a moderator/owner
    SPAM = 5      # Marked as spam
    DELETED = 6   # Deleted, but has children and hierarchy needs to be preserved


class CsrfForm(wtf.Form):
    """
    CSRF validation only
    """
    pass


class CommentForm(wtf.Form):
    """
    Comment form
    """
    reply_to_id = wtf.HiddenField("Parent", default='', id='comment_reply_to_id')
    edit_id = wtf.HiddenField("Edit", default='', id='comment_edit_id')
    message = wtf.TextAreaField("Add comment", id='comment_message', validators=[wtf.Required()])


class CommenteaseActionError(Exception):
    pass


class VotingMixin(object):
    @declared_attr
    def votes_id(cls):
        return Column(None, ForeignKey('voteset.id'), nullable=True)

    @declared_attr
    def votes(cls):
        return relationship('VoteSet', lazy='joined', single_parent=True,
            backref=backref(cls.__tablename__ + '_parent', cascade='all, delete-orphan'))

    #: Allow voting? This flag allows voting to be turned off if required
    @declared_attr
    def allow_voting(cls):
        return Column(Boolean, nullable=False, default=False)


class CommentingMixin(object):
    @declared_attr
    def comments_id(cls):
        return Column(None, ForeignKey('commentset.id'), nullable=True)

    @declared_attr
    def comments(cls):
        return relationship('CommentSet', lazy='subquery', single_parent=True,
            backref=backref(cls.__tablename__ + '_parent', cascade='all, delete-orphan'))

    #: Allow comments? This flag allows commenting to be turned off if required
    @declared_attr
    def allow_commenting(cls):
        return Column(Boolean, nullable=False, default=False)


class Commentease(object):
    """
    Flask extension for comments and votes on any database model
    """
    def __init__(self, app=None, db=None):
        self.app = app
        self.db = db

        self.CsrfForm = CsrfForm
        self.CommentForm = CommentForm

        self.sanitize_tags = ['p', 'br', 'strong', 'em', 'sup', 'sub', 'h3', 'h4', 'h5', 'h6',
                'ul', 'ol', 'li', 'a', 'blockquote', 'code']
        self.sanitize_attributes = {'a': ['href', 'title', 'target']}

        self.parsers = {
            'html': self.sanitize,
            'markdown': Markdown(safe_mode='escape', output_format='html5',
                                 extensions=['codehilite'],
                                 extension_configs={'codehilite': {'css_class': 'syntax'}}
                                 ).convert
            }

        if app is not None:
            self.init_app(app)

        if db is not None:
            self.init_db(db)

    def sanitize(self, text):
        """
        Sanitize HTML to remove harmful tags and attributes.
        """
        return bleach.clean(text, tags=self.sanitize_tags, attributes=self.sanitize_attributes)

    def cook(self, parser, text):
        """
        Cook text with the specified parser.
        """
        return self.parsers[parser](text)

    def init_app(self, app):
        self.app = app
        if 'COMMENT_TAGS' in app.config:
            self.sanitize_tags = app.config['COMMENT_TAGS']
        if 'COMMENT_ATTRIBUTES' in app.config:
            self.sanitize_attributes = app.config['COMMENT_ATTRIBUTES']

    def init_db(self, db, userid='user.id', usermodel='User'):
        self.db = db

        # Create models that are linked to this database object
        class Vote(TimestampMixin, db.Model):
            __tablename__ = 'vote'
            #: Id of user who voted
            user_id = db.Column(None, db.ForeignKey(userid), nullable=False, primary_key=True)
            #: User who voted
            user = db.relationship(usermodel,
                backref=db.backref('votes', cascade="all, delete-orphan"))
            #: Id of voteset
            voteset_id = db.Column(None, db.ForeignKey('voteset.id'), nullable=False, primary_key=True)
            #: VoteSet this vote is a part of
            voteset = db.relationship('VoteSet',
                backref=db.backref('votes', cascade="all, delete-orphan"))
            #: Voting data. Contents vary based on voting pattern (boolean, range, flags)
            data = db.Column(db.Integer, nullable=True)

        class VoteSet(BaseMixin, db.Model):
            __tablename__ = 'voteset'
            #: Type of entity getting voted on
            type = db.Column(db.Unicode(4), nullable=True)
            #: Number of votes
            count = db.Column(db.Integer, default=0, nullable=False)
            #: Voting score (sum of votes)
            score = db.Column(db.Integer, default=0, nullable=False)
            #: Voting average
            average = db.column_property(score / count)
            #: Voting pattern
            pattern = db.Column(db.SmallInteger, nullable=False, default=VOTE_PATTERN.UP_DOWN)
            #: Range vote minimum (optional)
            min = db.Column(db.Integer, nullable=True)
            #: Range vote maximum (optional)
            max = db.Column(db.Integer, nullable=True)

            def __init__(self, **kwargs):
                super(VoteSet, self).__init__(**kwargs)
                self.count = 0
                self.score = 0

            def vote(self, user, data=None):
                vote = self.getvote(user)
                # The following count and score incrementing operations work with SQL expressions
                # rather than actual values to handle concurrency. Offloading calculations to the
                # database prevents concurrent updates from messing up the values.
                if self.pattern == VOTE_PATTERN.UP_ONLY:
                    if data is not None:
                        raise ValueError("Invalid vote data: %s." % data)
                    if not vote:
                        vote = Vote(user=user, voteset=self)
                        db.session.add(vote)
                        self.count = self.__table__.c.count + 1
                        self.score = self.__table__.c.score + 1
                elif self.pattern == VOTE_PATTERN.UP_DOWN:
                    if data not in [+1, -1]:
                        raise ValueError("Invalid vote data: %s." % data)
                    if not vote:
                        vote = Vote(user=user, voteset=self, data=data)
                        db.session.add(vote)
                        self.count = self.__table__.c.count + 1
                        self.score = self.__table__.c.score + data
                    else:
                        if data != vote.data:
                            # Recalculate score
                            vote.data = data
                            self.score = self.__table__.c.score + (data * 2)
                elif self.pattern == VOTE_PATTERN.RANGE:
                    if self.min <= data <= self.max:
                        raise ValueError("Vote data out of range %d <= %d <= %d." % (
                            self.min, data, self.max))
                    if not vote:
                        vote = Vote(user=user, voteset=self, data=data)
                        db.session.add(vote)
                        self.count = self.__table__.c.count + 1
                        self.score = self.__table__.c.score + data
                    else:
                        if data != vote.data:
                            self.score = self.__table__.c.score - vote.data + data
                            vote.data = data
                elif self.pattern == VOTE_PATTERN.CUSTOM:
                    if not vote:
                        vote = Vote(user=user, voteset=self, data=data)
                        self.db.session.add(vote)
                        self.count = self.__table__.c.count + 1
                    else:
                        vote.data = data
                    # We don't know how to calculate score for custom votes. Return vote
                    # and let the caller manage the score.
                else:
                    # This shouldn't happen. This VoteSet has an invalid pattern type.
                    raise ValueError("Unknown voting pattern.")
                return vote

            def cancelvote(self, user):
                vote = self.getvote(user)
                if vote:
                    self.count = self.__table__.c.count - 1
                    if self.pattern == VOTE_PATTERN.UP_ONLY:
                        self.score = self.__table__.c.score - 1
                    elif self.pattern in (VOTE_PATTERN.UP_DOWN, VOTE_PATTERN.RANGE):
                        self.score = self.__table__.c.score - vote.data
                    elif self.pattern == VOTE_PATTERN.CUSTOM:
                        pass  # Do nothing. App maintains score.
                    else:
                        # This shouldn't happen. This VoteSet has an invalid pattern type.
                        raise ValueError("Unknown voting pattern.")
                    db.session.delete(vote)

            def recount(self):
                self.count = len(self.votes)
                if self.pattern != VOTE_PATTERN.CUSTOM:
                    self.score = sum([v.data for v in self.votes])

            def getvote(self, user):
                return Vote.query.get(user.id, self.id)

        class Comment(BaseScopedIdMixin, db.Model):
            __tablename__ = 'comment'
            user_id = db.Column(db.Integer, db.ForeignKey(userid), nullable=True)
            user = db.relationship(usermodel,
                backref=db.backref('comments', cascade="all"))
            commentset_id = db.Column(db.Integer, db.ForeignKey('commentset.id'), nullable=False)
            commentset = db.relationship('CommentSet',
                backref=db.backref('comments', cascade="all, delete-orphan"))

            # TODO: Remove this and use CommentTree.
            reply_to_id = db.Column(db.Integer, db.ForeignKey('comment.id'), nullable=True)
            replies = db.relationship('Comment', backref=db.backref("reply_to", remote_side='Comment.id'))

            parser = db.Column(db.Unicode(10), default=u'markdown', nullable=False)
            message = db.Column(db.Text, nullable=False)
            _message_html = db.Column('message_html', db.Text, nullable=False)

            status = db.Column(db.SmallInteger, default=0, nullable=False)

            votes_id = db.Column(db.Integer, db.ForeignKey('voteset.id'), nullable=False)
            votes = db.relationship(VoteSet, uselist=False)

            edited_at = db.Column(db.DateTime, nullable=True)

            def __init__(self, downvoting=True, **kwargs):
                super(Comment, self).__init__(**kwargs)
                self.votes = VoteSet(type=u'CMNT', downvoting=downvoting)

            @synonym_for("_message_html")
            @property
            def message_html(self):
                return Markup(self._message_html)

            def delete(self):
                """
                Delete this comment.
                """
                if len(self.replies) > 0:
                    self.status = COMMENT_STATUS.DELETED
                    self.user = None
                    self.message = ''
                    self.message_html = ''
                else:
                    if self.reply_to and self.reply_to.is_deleted:
                        # If the parent (reply_to) is deleted, ask it to reconsider removing itself
                        reply_to = self.reply_to
                        reply_to.replies.remove(self)
                        db.session.delete(self)
                        reply_to.delete()
                    else:
                        db.session.delete(self)

            @property
            def is_deleted(self):
                return self.status == COMMENT_STATUS.DELETED

            def sorted_replies(self):
                return sorted(self.replies, key=lambda reply: reply.votes.count)

        class CommentSet(BaseMixin, db.Model):
            __tablename__ = 'commentset'
            #: Type of entity being voted on
            type = db.Column(db.Unicode(4), nullable=True)
            #: Number of comments
            count = db.Column(db.Integer, default=0, nullable=False)
            #: Is downvoting a comment allowed? (selects voting pattern)
            downvoting = db.Column(db.Boolean, nullable=False, default=True)

            def __init__(self, **kwargs):
                super(CommentSet, self).__init__(**kwargs)
                self.count = 0

            def recount(self):
                self.count = sum(1 for c in self.comments if c.status == COMMENT_STATUS.PUBLIC)

        class CommentTree(db.Model):
            __tablename__ = 'comment_tree'
            #: Parent comment id
            parent_id = db.Column(None, db.ForeignKey('comment.id'), nullable=False, primary_key=True)
            #: Parent comment
            parent = db.relationship(Comment, primaryjoin=parent_id == Comment.id,
                backref=db.backref('childtree', cascade='all, delete-orphan'))
            #: Child comment id
            child_id = db.Column(None, db.ForeignKey('comment.id'), nullable=False, primary_key=True)
            #: Child comment
            child = db.relationship(Comment, primaryjoin=child_id == Comment.id,
                backref=db.backref('parenttree', cascade='all, delete-orphan'))
            #: Path length
            path_length = db.Column(db.SmallInteger, nullable=False)

        self.Vote = Vote
        self.VoteSet = VoteSet
        self.Comment = Comment
        self.CommentSet = CommentSet
        self.CommentTree = CommentTree

    # View handlers. These functions consolidate all possible actions on a voteset
    # or commentset into one handler each. Implementing apps must provide one view
    # handler for each that performs the necessary basic validations (such as access
    # permissions), looks up the appropriate voteset or commentset, and calls
    # these handlers to implement the rest.

    # Vote view handler
    def vote_action(self, voteset):
        if request.method == 'POST':
            form = CsrfForm()
            if form.validate():
                action = request.form.get('action')
                if action == 'vote':
                    # TODO: Vote up
                    pass
                elif action == 'votedown':
                    # TODO: Vote down
                    pass
                else:
                    raise CommenteaseActionError(u"Unknown voting action")
            else:
                return form

    # Comment view handler
    def comment_action(self, commentset):
        pass
