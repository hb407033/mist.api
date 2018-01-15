from datetime import datetime

from mist.api.keys.models import Key
from mist.api.clouds.models import Cloud
from mist.api.machines.models import Machine

from mist.api.tag.methods import get_tags_for_resource

from mist.api.helpers import trigger_session_update
from mist.api.helpers import transform_key_machine_associations

from mist.api import config

import logging

logging.basicConfig(level=config.PY_LOG_LEVEL,
                    format=config.PY_LOG_FORMAT,
                    datefmt=config.PY_LOG_FORMAT_DATE)
log = logging.getLogger(__name__)


def delete_key(owner, key_id):
    """Deletes given key.
    If key was default, then it checks if there are still keys left
    and assigns another one as default.

    :param owner:
    :param key_id:
    :return:
    """
    log.info("Deleting key with id '%s'.", key_id)
    key = Key.objects.get(owner=owner, id=key_id, deleted=None)
    default_key = key.default
    key.update(set__deleted=datetime.utcnow())
    other_key = Key.objects(owner=owner, id__ne=key_id, deleted=None).first()
    if default_key and other_key:
        other_key.default = True
        other_key.save()

    log.info("Deleted key with id '%s'.", key_id)
    trigger_session_update(owner, ['keys'])


def list_keys(owner):
    """List owner's keys
    :param owner:
    :return:
    """
    keys = Key.objects(owner=owner, deleted=None)
    clouds = Cloud.objects(owner=owner, deleted=None)
    key_objects = []
    # FIXME: This must be taken care of in Keys.as_dict
    for key in keys:
        key_object = {}
        # FIXME: Need to optimize this! It's potentially invoked per ssh probe.
        # Can't we expose associations directly from Machine.key_associations?
        machines = Machine.objects(cloud__in=clouds,
                                   key_associations__keypair__exact=key)
        key_object["id"] = key.id
        key_object['name'] = key.name
        key_object["isDefault"] = key.default
        key_object["machines"] = transform_key_machine_associations(machines,
                                                                    key)
        key_object['tags'] = get_tags_for_resource(owner, key)
        key_objects.append(key_object)
    return key_objects


# SEC
def filter_list_keys(auth_context, perm='read'):
    """Returns of a list of keys. The list is filtered for non-Owners based on
    the permissions granted.
    """
    keys = list_keys(auth_context.owner)
    if not auth_context.is_owner():
        keys = [key for key in keys if key['id'] in
                auth_context.get_allowed_resources(rtype='keys')]
    return keys
