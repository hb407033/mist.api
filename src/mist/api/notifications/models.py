import uuid
import json
import urllib
import datetime
import mongoengine as me

from mist.api import config

from mist.api.helpers import encrypt
from mist.api.helpers import mac_sign

from mist.api.users.models import User
from mist.api.machines.models import Machine

import mist.api.notifications.channels as cnls


# TODO Move to config.py
DEFAULT_REMINDER_SCHEDULE = [
    0,
    60 * 10,
    60 * 60,
    60 * 60 * 24,
]


class NotificationOverride(me.EmbeddedDocument):
    """A user override that blocks notifications with given properties."""

    id = me.StringField(default=lambda: uuid.uuid4().hex)

    rid = me.StringField(default="")
    rtype = me.StringField(default="")
    channel = me.StringField(default="")

    def blocks_ntf(self, ntf):
        """Return True if self blocks the given notification."""
        if self.channel and self.channel != ntf.channel.ctype:
            return False
        if self.rtype and self.rtype != ntf.rtype:
            return False
        if self.rid and self.rid != ntf.rid:
            return False
        return True

    def blocks_channel(self, channel):
        """Return True if self completely blocks the given channel."""
        return channel == self.channel and not (self.rtype or self.rid)

    def clean(self):
        if self.rid and not self.rtype:
            raise me.ValidationError('Resource ID provided without a type')
        if self.rtype:
            self.rtype = self.rtype.rstrip('s')

    # FIXME All following methods/properties are for backwards compatibility.

    @property
    def machine(self):
        return Machine.objects.get(id=self.rid)

    @property
    def cloud(self):
        return self.machine.cloud

    @property
    def value(self):
        return "BLOCK"

    def as_dict(self):
        machine = self.machine
        return {
            '_id': {'$oid': self.id},
            'rid': self.rid,
            'rtype': self.rtype,
            'channel': self.channel,
            'machine': {"_ref": {"$ref": "machines", "$id": machine.id}},
            'cloud': {"_ref": {"$ref": "clouds", "$id": machine.cloud.id}},
            'value': self.value,
        }


class UserNotificationPolicy(me.Document):
    """A user's notification policy comprised of notification overrides."""

    owner = me.ReferenceField('Organization', required=True)
    email = me.EmailField()
    user_id = me.StringField()

    overrides = me.EmbeddedDocumentListField(NotificationOverride)

    meta = {
        'collection': 'notification_policies',
        'indexes': [
            {
                'fields': ['user_id', 'owner'],
                'sparse': False,
                'unique': True,
                'cls': False,
            },
        ],
    }

    @property
    def user(self):
        return User.objects(me.Q(id=self.user_id) |
                            me.Q(email=self.email)).first()

    def has_blocked(self, ntf):
        """Return True if self blocks the given notification."""
        return self.has_dismissed(ntf) or self.has_overriden(ntf)

    def has_overriden(self, ntf):
        """Return True if self includes an override that matches `ntf`."""
        for override in self.overrides:
            if override.blocks_ntf(ntf):
                return True
        return False

    def has_dismissed(self, ntf):
        """Return True if the given notification has been dismissed."""
        if not isinstance(ntf, InAppNotification):
            return False
        return self.user_id in ntf.dismissed_by

    def clean(self):
        if not (self.email or self.user_id):
            raise me.ValidationError()

        # Get the user's id, if missing. Some notification policies may
        # belong to non-mist users (denoted by their e-mail).
        if not self.user_id:
            user = self.user
            self.user_id = user.id if user else None
        elif not self.email:
            self.email = self.user.email


class Notification(me.Document):
    """The main Notification entity."""

    id = me.StringField(primary_key=True, default=lambda: uuid.uuid4().hex)
    owner = me.ReferenceField('Organization', required=True)

    # TODO A list of external email addresses, ie e-mail addresses that do
    # not correspond to members of the organization.
    # emails = me.ListField(me.EmailField)

    # Content fields.
    subject = me.StringField(required=False, default="", max_length=512)
    text_body = me.StringField(required=True, default="")
    html_body = me.StringField(required=False, default="")

    # Taxonomy fields.
    rid = me.StringField(default="")
    rtype = me.StringField(default="")

    # Reminder fields.
    reminder_count = me.IntField(required=True, min_value=0, default=0)
    reminder_enabled = me.BooleanField()
    reminder_schedule = me.ListField(default=DEFAULT_REMINDER_SCHEDULE)

    created_at = me.DateTimeField(default=lambda: datetime.datetime.utcnow())

    meta = {
        'strict': False,
        'allow_inheritance': True,
        'collection': 'notifications',
        'indexes': ['owner', '-created_at'],
    }

    _notification_channel_cls = None

    def __init__(self, *args, **kwargs):
        super(Notification, self).__init__(*args, **kwargs)
        if not self._notification_channel_cls:
            raise TypeError("Can't initialize %s. This is just a base class "
                            "and shouldn't be used to create notifications. "
                            "Use a subclass that defines a "
                            "`_notification_channel_cls` attribute" % self)
        self.channel = self._notification_channel_cls(self)

    @property
    def remind_in(self):
        """Return a timedelta until the next reminder since `created_at`."""
        remind_in = self.reminder_schedule[self.reminder_count]
        return datetime.timedelta(seconds=remind_in)

    def due_in(self):
        """Return a countdown until the next alert (reminder) is due."""
        return self.created_at + self.remind_in - datetime.datetime.utcnow()

    def is_due(self):
        """Return True if self is due."""
        if not self.reminder_enabled and self.reminder_count:
            return False
        if self.reminder_count > len(self.reminder_schedule):
            return False
        if self.due_in().total_seconds() > 0:
            return False
        return True

    def clean(self):
        # This makes sure to fast-forward the `reminder_count` in case we've
        # failed to send past notifications for periods of time that span
        # reminder intervals. Thus we avoid spamming users with back-to-back
        # reminders.
        schedule_size = len(self.reminder_schedule)
        for c in xrange(schedule_size - 1, self.reminder_count, -1):
            timedelta = datetime.timedelta(seconds=self.reminder_schedule[c])
            if self.created_at + timedelta < datetime.datetime.utcnow():
                self.reminder_count = c
                break

    # FIXME All following methods/properties are for backwards compatibility.

    @property
    def machine(self):
        if self.rtype != 'machine':
            return None
        return Machine.objects.get(id=self.rid)

    @property
    def cloud(self):
        return self.machine.cloud if self.rtype == 'machine' else None

    @property
    def source(self):
        return self.channel.ctype

    @property
    def created_at_int(self):
        return int(self.created_at.strftime('%s')) * 1000

    def as_dict(self):
        machine = self.machine
        return {
            '_id': self.id,
            'source': self.source,
            'summary': self.subject,
            'subject': self.subject,
            'body': self.text_body,
            'html_body': self.html_body,
            'machine': {"_ref": {"$ref": "machines", "$id": machine.id}},
            'cloud': {"_ref": {"$ref": "clouds", "$id": machine.cloud.id}},
            'created_date': {"$date": self.created_at_int},
        }


class EmailNotification(Notification):

    _notification_channel_cls = cnls.EmailNotificationChannel

    # E-mail "FROM" and "Title" fields. Not db fields, just class attributes.
    sender_title = "Mist.io Notifications"
    sender_email = config.EMAIL_NOTIFICATIONS

    def __init__(self, *args, **kwargs):
        super(EmailNotification, self).__init__(*args, **kwargs)
        if not self.sender_title:
            raise TypeError("%s requires the e-mail's title to be specified as"
                            " the sender_title class attribute" % self)
        if not self.sender_email:
            raise TypeError("%s requires the sender's email to be specified as"
                            " the sender_email class attribute" % self)

    def get_unsub_link(self, user_id, email=None):
        params = {'action': 'request_unsubscribe',
                  'channel': self.channel.ctype, 'org_id': self.owner.id,
                  'user_id': user_id, 'email': email}
        mac_sign(params)
        encrypted = {'token': encrypt(json.dumps(params))}
        return '%s/unsubscribe?%s' % (config.CORE_URI,
                                      urllib.urlencode(encrypted))


class EmailReport(EmailNotification):

    sender_title = "Mist.io Reports"
    sender_email = config.EMAIL_REPORTS


class EmailAlert(EmailNotification):

    sender_title = "Mist.io Alerts"
    sender_email = config.EMAIL_ALERTS

    # The ID associated with a specific incident.
    # Required in order to schedule alerts via e-mail notifications.
    incident_id = me.StringField(required=True)


class InAppNotification(Notification):

    _notification_channel_cls = cnls.InAppNotificationChannel

    # List of users that dismissed this notification.
    dismissed_by = me.ListField(me.StringField())


class InAppRecommendation(InAppNotification):

    # Fields specific to recommendations.
    model_id = me.StringField(required=True)
    model_output = me.DictField(required=True, default={})

    # List of users that applied this recommendation.
    applied = me.BooleanField(required=True, default=False)

    def as_dict(self):
        d = super(InAppRecommendation, self).as_dict()
        d.update({'model_id': self.model_id,
                  'model_output': self.model_output})
        return d
