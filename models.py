import datetime
from datetime import timezone
import decimal
import logging
import io
import json

from flask import redirect, url_for, request, abort, flash, has_app_context, g
from flask_admin import expose
from flask_admin.actions import action
from flask_admin.babel import lazy_gettext
from flask_admin.model import filters
from flask_admin.contrib import sqla
from sqlalchemy import and_
from flask_admin.contrib.sqla.filters import BaseSQLAFilter
from wtforms import ValidationError
from flask_security import Security, SQLAlchemyUserDatastore, \
    UserMixin, RoleMixin, login_required, current_user
from marshmallow import Schema, fields
from markupsafe import Markup

from app_core import app, db
from utils import generate_key

logger = logging.getLogger(__name__)

#
# Define models
#

roles_users = db.Table(
    'roles_users',
    db.Column('user_id', db.Integer(), db.ForeignKey('user.id')),
    db.Column('role_id', db.Integer(), db.ForeignKey('role.id')))

class Role(db.Model, RoleMixin):
    id = db.Column(db.Integer(), primary_key=True)
    name = db.Column(db.String(80), unique=True)
    description = db.Column(db.String(255))

    @classmethod
    def from_name(cls, session, name):
        return session.query(cls).filter(cls.name == name).first()

    def __str__(self):
        return self.name

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    merchant_name = db.Column(db.String(255))
    merchant_code = db.Column(db.String(255), unique=True)
    email = db.Column(db.String(255), unique=True)
    password = db.Column(db.String(255))
    active = db.Column(db.Boolean())
    confirmed_at = db.Column(db.DateTime())
    roles = db.relationship('Role', secondary=roles_users,
                            backref=db.backref('users', lazy='dynamic'))
    max_settlements_per_month = db.Column(db.Integer)
    merchant_rate = db.Column(db.Numeric)
    customer_rate = db.Column(db.Numeric)
    wallet_address = db.Column(db.String(255))

    def __init__(self, **kwargs):
        self.merchant_code = generate_key(4)
        super().__init__(**kwargs)

    @classmethod
    def from_email(cls, session, email):
        return session.query(cls).filter(cls.email == email).first()

    @classmethod
    def all(cls, session):
        return session.query(cls).all()

    def __str__(self):
        return '%s (%s)' % (self.merchant_code, self.merchant_name)

class InvoiceSchema(Schema):
    date = fields.Float()
    token = fields.String()
    nonce = fields.Integer()
    secret = fields.String()
    amount = fields.Integer()
    amount_nzd = fields.Integer()
    state = fields.String()

class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.DateTime(), nullable=False)
    token = db.Column(db.String(255), unique=True, nullable=False)
    nonce = db.Column(db.Integer, nullable=False)
    secret = db.Column(db.String(255), nullable=False)
    #TODO: invoice details (amount, amount nzd, state etc..)

    def __init__(self, name):
        self.generate_defaults()

    def generate_defaults(self):
        self.date = datetime.datetime.now()
        self.token = generate_key(8)
        self.nonce = 0
        self.secret = generate_key(16)

    @classmethod
    def count(cls, session):
        return session.query(cls).count()

    @classmethod
    def from_token(cls, session, token):
        return session.query(cls).filter(cls.token == token).first()

    def __repr__(self):
        return "<Invoice %r>" % (self.token)

    def to_json(self):
        schema = InvoiceSchema()
        return schema.dump(self).data

#
# Setup Flask-Security
#

user_datastore = SQLAlchemyUserDatastore(db, User, Role)
security = Security(app, user_datastore)

#
# Define model views
#

def _format_amount(view, context, model, name):
    if name == 'amount':
        return Markup(model.amount / 100)
    if name == 'amount_nzd':
        return round((model.amount_nzd / 100),2)

class ReloadingIterator:
    def __init__(self, iterator_factory):
        self.iterator_factory = iterator_factory

    def __iter__(self):
        return self.iterator_factory()

class BaseModelView(sqla.ModelView):
    def _handle_view(self, name, **kwargs):
        """
        Override builtin _handle_view in order to redirect users when a view is not accessible.
        """
        if not self.is_accessible():
            if current_user.is_authenticated:
                # permission denied
                abort(403)
            else:
                # login
                return redirect(url_for('security.login', next=request.url))

class RestrictedModelView(BaseModelView):
    can_create = False
    can_delete = False
    can_edit = False
    column_exclude_list = ['password', 'secret']

    def is_accessible(self):
        return (current_user.is_active and
                current_user.is_authenticated and
                current_user.has_role('admin'))

class UserModelView(RestrictedModelView):
    can_create = False
    can_delete = False
    can_edit = False
    column_list = ['merchant_name', 'merchant_code', 'email', 'roles', 'max_settlements_per_month', 'merchant_rate', 'customer_rate', 'wallet_address']
    column_editable_list = ['merchant_name', 'roles', 'max_settlements_per_month', 'merchant_rate', 'customer_rate']

class InvoiceModelView(RestrictedModelView):
    can_create = False
    can_delete = False
    can_edit = False

    column_formatters = dict(amount=_format_amount, amount_receive=_format_amount)
    column_labels = dict(amount='ZAP Amount', amount_receive='NZD Amount')