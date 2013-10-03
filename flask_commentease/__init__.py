# -*- coding: utf-8 -*-
"""
    flask.ext.commentease
    ~~~~~~~~~~~~~~~~~~~~~

    Flask extension for comments and votes on any database model

    :copyright: (c) 2011-13 by HasGeek Media LLP.
    :license: BSD, see LICENSE for more details.
"""

import bleach
from datetime import datetime
from flask import g, Blueprint, Markup, request, flash, redirect
from sqlalchemy import Column, ForeignKey, Boolean
from sqlalchemy.sql import select
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.declarative import declared_attr
import wtforms
from coaster.gfm import markdown
from coaster.sqlalchemy import TimestampMixin, BaseMixin, BaseScopedIdMixin
from baseframe import assets, Version
from baseframe.forms import Form
from ._version import __version__

__all__ = ['Commentease', 'CommentingMixin', 'VotingMixin', 'CommenteaseActionError']

version = Version(__version__)
#assets['commentease.js'][version] = 'commentease/js/commentease.js'
assets['commentease.css'][version] = 'commentease/css/commentease.css'


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


class CsrfForm(Form):
    """
    CSRF validation only
    """
    pass


class CommentForm(Form):
    """
    Comment form
    """
    comment_reply_to_id = wtforms.HiddenField("Reply to", default='', id='comment_reply_to_id')
    comment_edit_id = wtforms.HiddenField("Edit", default='', id='comment_edit_id')
    message = wtforms.TextAreaField("Add comment", id='comment_message', validators=[wtforms.validators.Required()])


class DeleteCommentForm(Form):
    comment_id = wtforms.HiddenField('Comment', validators=[wtforms.validators.Required()])


class CommenteaseActionError(Exception):
    pass


class VotingMixin(object):
    @declared_attr
    def votes_id(cls):
        return Column(None, ForeignKey('voteset.id'), nullable=True)

    @declared_attr
    def votes(cls):
        return relationship('VoteSet', lazy='joined', single_parent=True,
            backref=backref(cls.__tablename__ + '_parent'), cascade='all, delete-orphan')

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
            backref=backref(cls.__tablename__ + '_parent'), cascade='all, delete-orphan')

    #: Allow comments? This flag allows commenting to be turned off if required
    @declared_attr
    def allow_commenting(cls):
        return Column(Boolean, nullable=False, default=False)


commentease_blueprint = Blueprint('commentease', __name__,
    static_folder='static',
    static_url_path='/static/commentease',
    template_folder='templates')


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
            'markdown': markdown
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
        app.register_blueprint(commentease_blueprint)

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
            # average = db.column_property(score / count)  # Causes division by zero errors
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
                        if self.id is not None:
                            self.count = select([self.__table__.c.count]).where(
                                self.__table__.c.id == self.id).as_scalar() + 1
                            self.score = select([self.__table__.c.score]).where(
                                self.__table__.c.id == self.id).as_scalar() + 1
                        else:
                            self.count = 1
                            self.score = data
                elif self.pattern == VOTE_PATTERN.UP_DOWN:
                    if data not in [+1, -1]:
                        raise ValueError("Invalid vote data: %s." % data)
                    if not vote:
                        vote = Vote(user=user, voteset=self, data=data)
                        db.session.add(vote)
                        if self.id is not None:
                            self.count = select([self.__table__.c.count]).where(
                                self.__table__.c.id == self.id).as_scalar() + 1
                            self.score = select([self.__table__.c.score]).where(
                                self.__table__.c.id == self.id).as_scalar() + data
                        else:
                            self.count = 1
                            self.score = data
                    else:
                        if data != vote.data:
                            # Recalculate score
                            vote.data = data
                            if self.id is not None:
                                self.score = select([self.__table__.c.score]).where(
                                    self.__table__.c.id == self.id).as_scalar() + (data * 2)
                            else:
                                # We should never get here. Can't have vote but no voteset
                                self.score = data
                elif self.pattern == VOTE_PATTERN.RANGE:
                    if self.min <= data <= self.max:
                        raise ValueError("Vote data out of range %d <= %d <= %d." % (
                            self.min, data, self.max))
                    if not vote:
                        vote = Vote(user=user, voteset=self, data=data)
                        db.session.add(vote)
                        if self.id is not None:
                            self.count = select([self.__table__.c.count]).where(
                                self.__table__.c.id == self.id).as_scalar() + 1
                            self.score = select([self.__table__.c.score]).where(
                                self.__table__.c.id == self.id).as_scalar() + data
                        else:
                            self.count = 1
                            self.score = data
                    else:
                        if data != vote.data:
                            self.score = select([self.__table__.c.score]).where(
                                self.__table__.c.id == self.id).as_scalar() - vote.data + data
                            vote.data = data
                elif self.pattern == VOTE_PATTERN.CUSTOM:
                    if not vote:
                        vote = Vote(user=user, voteset=self, data=data)
                        self.db.session.add(vote)
                        if self.id is not None:
                            self.count = select([self.__table__.c.count]).where(
                                self.__table__.c.id == self.id).as_scalar() + 1
                        else:
                            self.count = 1
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
                return Vote.query.get((user.id, self.id))

        class Comment(BaseScopedIdMixin, db.Model):
            __tablename__ = 'comment'
            user_id = db.Column(db.Integer, db.ForeignKey(userid), nullable=True)
            user = db.relationship(usermodel,
                backref=db.backref('comments', cascade="all"))
            commentset_id = db.Column(db.Integer, db.ForeignKey('commentset.id'), nullable=False)
            commentset = db.relationship('CommentSet',
                backref=db.backref('comments', cascade="all, delete-orphan"))
            parent = db.synonym('commentset')

            # TODO: Remove this and use CommentTree.
            reply_to_id = db.Column(db.Integer, db.ForeignKey('comment.id'), nullable=True)
            replies = db.relationship('Comment', backref=db.backref("reply_to", remote_side='Comment.id'))

            parser = db.Column(db.Unicode(10), default=u'markdown', nullable=False)
            _message = db.Column('message', db.UnicodeText, nullable=False)
            _message_html = db.Column('message_html', db.UnicodeText, nullable=False)

            status = db.Column(db.SmallInteger, default=0, nullable=False)

            votes_id = db.Column(db.Integer, db.ForeignKey('voteset.id'), nullable=False)
            votes = db.relationship(VoteSet, uselist=False)

            edited_at = db.Column(db.DateTime, nullable=True)

            def __init__(self, votepattern=VOTE_PATTERN.UP_DOWN, **kwargs):
                super(Comment, self).__init__(**kwargs)
                self.votes = VoteSet(type=u'CMNT', pattern=votepattern)

            @property
            def message(self):
                return self._message

            @message.setter
            def message(self, value):
                self._message = value
                self._message_html = markdown(value)

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
            #: Number of top-level comments
            count_toplevel = db.Column(db.Integer, default=0, nullable=False)
            #: Number of reply comments
            count_replies = db.Column(db.Integer, default=0, nullable=False)
            #: Is downvoting a comment allowed? (selects voting pattern)
            downvoting = db.Column(db.Boolean, nullable=False, default=True)

            def __init__(self, **kwargs):
                super(CommentSet, self).__init__(**kwargs)
                self.count = self.count_toplevel = self.count_replies = 0

            def recount(self):
                toplevel = replies = 0
                for comment in self.comments:
                    if comment.status == COMMENT_STATUS.PUBLIC:
                        if comment.reply_to is None:
                            toplevel += 1
                        else:
                            replies += 1

                self.count_toplevel = toplevel
                self.count_replies = replies
                self.count = toplevel + replies

        # TODO: Use this
        class CommentTree(TimestampMixin, db.Model):
            """
            The comment tree implements a closure set structure to help navigate up and down
            a hierarchical comment thread.
            """
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
            #: Distance from parent to child in the hierarchy
            depth = db.Column(db.SmallInteger, nullable=False)

        self.Vote = Vote
        self.VoteSet = VoteSet
        self.Comment = Comment
        self.CommentSet = CommentSet
        self.CommentTree = CommentTree

    def enable_voting(self, obj):
        if isinstance(obj, VotingMixin):
            obj.allow_voting = True
            if obj.votes is None:
                obj.votes = self.VoteSet()

    def enable_commenting(self, obj):
        """
        Enable commenting on the given object.
        """
        if isinstance(obj, CommentingMixin):
            obj.allow_commenting = True
            if obj.comments is None:
                obj.comments = self.CommentSet()

    # View handlers. These functions consolidate all possible actions on a voteset
    # or commentset into one handler each. Implementing apps must provide one view
    # handler for each that performs the necessary basic validations (such as access
    # permissions), looks up the appropriate voteset or commentset, and calls
    # these handlers to implement the rest.

    def forms(self):
        """
        Forms for the display templates.
        """
        return {
            'csrf': CsrfForm(),
            'comment': CommentForm(),
            'delcomment': DeleteCommentForm()
            }

    # Vote view handler
    def vote_action(self, voteset, user, permissions=None):
        permissions = voteset.permissions(user, permissions)
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
    def comment_action(self, commentset, user, permissions=None):
        permissions = commentset.permissions(user, permissions)
        if request.method == 'POST' and 'form.id' in request.form:
            # Look for form submission
            commentform = CommentForm()
            if request.form['form.id'] == 'newcomment' and commentform.validate():
                if commentform.comment_edit_id.data:
                    comment = self.Comment.query.get(int(commentform.comment_edit_id.data))
                    if comment:
                        if comment.user == g.user:
                            comment.message = commentform.message.data
                            comment.edited_at = datetime.utcnow()
                            flash("Your comment has been edited", "info")
                        else:
                            flash("You can only edit your own comments", "info")
                    else:
                        flash("No such comment", "error")
                else:
                    comment = self.Comment(user=g.user, commentset=commentset,
                        message=commentform.message.data)
                    if commentform.comment_reply_to_id.data:
                        reply_to = self.Comment.query.get(int(commentform.comment_reply_to_id.data))
                        if reply_to and reply_to.commentset == commentset:
                            comment.reply_to = reply_to
                    commentset.count += 1
                    if comment.votes.pattern == VOTE_PATTERN.UP_DOWN:
                        # Vote for your own comment
                        comment.votes.vote(g.user, +1)
                    self.db.session.add(comment)
                    flash("Your comment has been posted", "info")
                self.db.session.commit()
                return redirect(request.base_url)  # FIXME: Return form and new comment
        return "Form: %s %s" % (request.method, request.form)
