import re
import datetime

import mongoengine as me

from mist.api.tag.models import Tag


class BaseCondition(me.EmbeddedDocument):
    """Abstract Base class used as a common interface
    for condition types. There are four different types
    for now: FieldCondition, TaggingCondition, MachinesCondition
    and MachinesAgeCondition. """

    meta = {
        'allow_inheritance': True,
    }

    ctype = 'base'

    def update(self, **kwargs):
        for key, value in kwargs.iteritems():
            setattr(self, key, value)

    @property
    def q(self):
        return me.Q()

    def as_dict(self):
        return {'type': self.ctype}


class ConditionalClassMixin(object):
    """Generic condition mixin class used as a handler for different
    query sets for a specific collection. It constructs a query from
    a list of query sets which chains together with logical & operator."""

    condition_resource_cls = None  # Instance of mongoengine model class

    conditions = me.EmbeddedDocumentListField(BaseCondition)

    def owner_query(self):
        return me.Q(owner=self.owner_id)

    def get_resources(self):
        query = self.owner_query()
        for condition in self.conditions:
            query &= condition.q
        return self.condition_resource_cls.objects(query)

    def get_ids(self):
        return [resource.id for resource in self.get_resources().only('id')]


class FieldCondition(BaseCondition):
    """Generic condition for any field which is supported by specific
    collection."""

    ctype = 'field'

    field = me.StringField(required=True)
    value = me.DynamicField(required=True)
    operator = me.StringField(required=True, default='eq',
                              choices=('eq', 'ne', 'gt', 'lt'))

    @property
    def q(self):
        if self.operator == 'eq':
            return me.Q(**{self.field: self.value})
        return me.Q(**{'%s__%s' % (self.field, self.operator): self.value})

    def as_dict(self):
        return {'type': self.ctype, 'field': self.field,
                'value': self.value, 'operator': self.operator}


class TaggingCondition(BaseCondition):

    ctype = 'tags'

    tags = me.DictField(required=True, default=lambda: {})

    @property
    def q(self):
        rtype = self._instance.condition_resource_cls._meta["collection"]
        ids = set()
        for key, value in self.tags.iteritems():
            query = {
                'owner': self._instance.owner,
                'resource_type': rtype,
                'key': key,
            }
            if value:
                query['value'] = value
            ids |= set(tag.resource.id for tag in Tag.objects(**query))
        return me.Q(id__in=ids)

    def validate(self, clean=True):
        if self.tags:
            regex = re.compile(r'^[a-z0-9_-]+$')
            for key, value in self.tags.iteritems():
                if not key:
                    raise me.ValidationError('You cannot add a tag '
                                             'without a key')
                elif not regex.match(key) or (value and
                                              not regex.match(value)):
                    raise me.ValidationError('Tags must be in key=value '
                                             'format and only contain the '
                                             'characters a-z, 0-9, _, -')
        super(TaggingCondition, self).validate(clean=True)

    def clean(self):
        if not self.tags:
            self.tags = {}
        elif not isinstance(self.tags, dict):
            raise me.ValidationError('Tags must be a dictionary')

    def __str__(self):
        return 'Tags: %s' % self.tags

    def as_dict(self):
        return {'type': self.ctype, 'tags': self.tags}


class MachinesCondition(BaseCondition):

    ctype = 'machines'

    ids = me.ListField(me.StringField(required=True), required=True)

    @property
    def q(self):
        return me.Q(id__in=self.ids)

    def as_dict(self):
        return {'type': self.ctype, 'ids': self.ids}


class MachinesAgeCondition(BaseCondition):
    """Condition which computes machine's age and queries
    for machines which are older than this age. """

    ctype = 'age'

    minutes = me.IntField(required=True)

    @property
    def q(self):
        d = datetime.datetime.now() - datetime.timedelta(minutes=self.minutes)
        return me.Q(created__lt=d)

    def as_dict(self):
        return {'type': self.ctype, 'minutes': self.minutes}
