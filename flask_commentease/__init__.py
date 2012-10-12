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
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declared_attr
from flask import Markup, request
import flask.ext.wtf as wtf
from coaster.sqlalchemy import BaseMixin

__all__ = ['Commentease', 'CommentingMixin', 'VotingMixin', 'CommenteaseActionError']


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
        return Column(None, ForeignKey('votespace.id'), nullable=True)

    @declared_attr
    def votes(cls):
        return relationship('VoteSpace', backref=cls.__tablename__, single_parent=True, cascade='all, delete-orphan')

    #: Allow voting? This flag allows voting to be turned off if required
    @declared_attr
    def allow_voting(cls):
        return Column(Boolean, nullable=False, default=False)


class CommentingMixin(object):
    @declared_attr
    def comments_id(cls):
        return Column(None, ForeignKey('commentspace.id'), nullable=True)

    @declared_attr
    def comments(cls):
        return relationship('CommentSpace', backref=cls.__tablename__, single_parent=True, cascade='all, delete-orphan')

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

    def init_db(self, db):
        self.db = db

        # Create models that are linked to this database object
        class Vote(BaseMixin, db.Model):
            __tablename__ = 'vote'
            user_id = db.Column(None, db.ForeignKey('user.id'), nullable=False)
            user = db.relationship('User',
                backref=db.backref('votes', cascade="all, delete-orphan"))
            votespace_id = db.Column(None, db.ForeignKey('votespace.id'), nullable=False)
            votespace = db.relationship('VoteSpace',
                backref=db.backref('votes', cascade="all, delete-orphan"))
            votedown = db.Column(db.Boolean, default=False, nullable=False)

            __table_args__ = (db.UniqueConstraint("user_id", "votespace_id"), {})

        class VoteSpace(BaseMixin, db.Model):
            __tablename__ = 'votespace'
            type = db.Column(db.Unicode(4), nullable=True)
            count = db.Column(db.Integer, default=0, nullable=False)
            downvoting = db.Column(db.Boolean, nullable=False, default=True)

            def __init__(self, **kwargs):
                super(VoteSpace, self).__init__(**kwargs)
                self.count = 0

            def vote(self, user, votedown=False):
                vote = Vote.query.filter_by(user=user, votespace=self).first()
                if votedown and not self.downvoting:
                    # Ignore downvotes if disallowed
                    return
                if not vote:
                    vote = Vote(user=user, votespace=self, votedown=votedown)
                    self.count += 1 if not votedown else -1
                    db.session.add(vote)
                else:
                    if vote.votedown != votedown:
                        self.count += 2 if not votedown else -2
                    vote.votedown = votedown
                return vote

            def cancelvote(self, user):
                vote = Vote.query.filter_by(user=user, votespace=self).first()
                if vote:
                    self.count += 1 if vote.votedown else -1
                    db.session.delete(vote)

            def recount(self):
                self.count = sum(+1 if v.votedown is False else -1 for v in self.votes)

            def getvote(self, user):
                return Vote.query.filter_by(user=user, votespace=self).first()

        class Comment(BaseMixin, db.Model):
            __tablename__ = 'comment'
            user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
            user = db.relationship('User',
                backref=db.backref('comments', cascade="all"))
            commentspace_id = db.Column(db.Integer, db.ForeignKey('commentspace.id'), nullable=False)
            commentspace = db.relationship('CommentSpace',
                backref=db.backref('comments', cascade="all, delete-orphan"))

            reply_to_id = db.Column(db.Integer, db.ForeignKey('comment.id'), nullable=True)
            replies = db.relationship('Comment', backref=db.backref("reply_to", remote_side='Comment.id'))

            parser = db.Column(db.Unicode(10), default=u'markdown', nullable=False)
            message = db.Column(db.Text, nullable=False)
            _message_html = db.Column('message_html', db.Text, nullable=False)

            status = db.Column(db.Integer, default=0, nullable=False)

            votes_id = db.Column(db.Integer, db.ForeignKey('votespace.id'), nullable=False)
            votes = db.relationship(VoteSpace, uselist=False)

            edited_at = db.Column(db.DateTime, nullable=True)

            def __init__(self, downvoting=True, **kwargs):
                super(Comment, self).__init__(**kwargs)
                self.votes = VoteSpace(type=u'CMNT', downvoting=downvoting)

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

        class CommentSpace(BaseMixin, db.Model):
            __tablename__ = 'commentspace'
            type = db.Column(db.Unicode(4), nullable=True)
            count = db.Column(db.Integer, default=0, nullable=False)
            downvoting = db.Column(db.Boolean, nullable=False, default=True)

            def __init__(self, **kwargs):
                super(CommentSpace, self).__init__(**kwargs)
                self.count = 0

            def recount(self):
                self.count = sum(1 for c in self.comments if c.status == COMMENT_STATUS.PUBLIC)

        self.VoteSpace = VoteSpace
        self.Vote = Vote
        self.CommentSpace = CommentSpace
        self.Comment = Comment

    # View handlers. These functions consolidate all possible actions on a votespace
    # or commentspace into one handler each. Implementing apps must provide one view
    # handler for each that performs the necessary basic validations (such as access
    # permissions), looks up the appropriate votespace or commentspace, and calls
    # these handlers to implement the rest.

    # Vote view handler
    def vote_action(self, votespace):
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
    def comment_action(self, commentspace):
        pass
