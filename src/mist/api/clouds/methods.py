import mist.api.clouds.models as cloud_models

from mist.api.clouds.models import Cloud
from mist.api.machines.models import Machine

from mist.api.helpers import trigger_session_update

from mist.api.exceptions import RequiredParameterMissingError
from mist.api.exceptions import BadRequestError, NotFoundError

from mist.api.poller.models import ListMachinesPollingSchedule

try:
    from mist.core.methods import enable_monitoring
    from mist.core.methods import disable_monitoring_cloud
except ImportError:
    from mist.api.dummy.methods import enable_monitoring
    from mist.api.dummy.methods import disable_monitoring_cloud

from mist.api import config

import logging

logging.basicConfig(level=config.PY_LOG_LEVEL,
                    format=config.PY_LOG_FORMAT,
                    datefmt=config.PY_LOG_FORMAT_DATE)

log = logging.getLogger(__name__)


def add_cloud_v_2(owner, title, provider, params):
    """Add cloud to owner"""

    # FIXME: Some of these should be explicit arguments, others shouldn't exist
    fail_on_error = params.pop('fail_on_error',
                               params.pop('remove_on_error', True))
    monitoring = params.pop('monitoring', False)
    params.pop('title', None)
    params.pop('provider', None)
    # Find proper Cloud subclass.
    if not provider:
        raise RequiredParameterMissingError("provider")
    log.info("Adding new cloud in provider '%s'", provider)
    if provider not in cloud_models.CLOUDS:
        raise BadRequestError("Invalid provider '%s'." % provider)
    cloud_cls = cloud_models.CLOUDS[provider]  # Class of Cloud model.

    # Add the cloud.
    cloud = cloud_cls.add(owner, title, fail_on_error=fail_on_error,
                          fail_on_invalid_params=False, **params)
    ret = {'cloud_id': cloud.id}
    if provider == 'bare_metal' and monitoring:
        # Let's overload this a bit more by also combining monitoring.
        machine = Machine.objects.get(cloud=cloud)

        ret['monitoring'] = enable_monitoring(
            owner, cloud.id, machine.machine_id,
            no_ssh=not (machine.os_type == 'unix' and
                        machine.key_associations)
        )

    # SEC
    owner.mapper.update(cloud)

    log.info("Cloud with id '%s' added succesfully.", cloud.id)
    trigger_session_update(owner, ['clouds'])
    c_count = Cloud.objects(owner=owner, deleted=None).count()
    if owner.clouds_count != c_count:
        owner.clouds_count = c_count
        owner.save()

    cloud.polling_interval = 1800  # 30 min * 60 sec/min
    cloud.save()
    ListMachinesPollingSchedule.add(cloud=cloud)

    return ret


def rename_cloud(owner, cloud_id, new_name):
    """Renames cloud with given cloud_id."""

    log.info("Renaming cloud: %s", cloud_id)
    cloud = Cloud.objects.get(owner=owner, id=cloud_id, deleted=None)
    cloud.ctl.rename(new_name)
    log.info("Succesfully renamed cloud '%s'", cloud_id)
    trigger_session_update(owner, ['clouds'])


def delete_cloud(owner, cloud_id):
    """Deletes cloud with given cloud_id."""

    log.info("Deleting cloud: %s", cloud_id)

    try:
        disable_monitoring_cloud(owner, cloud_id)
    except Exception as exc:
        log.warning("Couldn't disable monitoring before deleting cloud. "
                    "Error: %r", exc)

    try:
        cloud = Cloud.objects.get(owner=owner, id=cloud_id, deleted=None)
        cloud.ctl.delete()
    except Cloud.DoesNotExist:
        raise NotFoundError('Cloud does not exist')

    log.info("Successfully deleted cloud '%s'", cloud_id)
    trigger_session_update(owner, ['clouds'])
    c_count = Cloud.objects(owner=owner, deleted=None).count()
    if owner.clouds_count != c_count:
        owner.clouds_count = c_count
        owner.save()


# SEC
def filter_list_clouds(auth_context, perm='read'):
    """Returns a list of clouds, which is filtered based on RBAC Mappings for
    non-Owners.
    """
    clouds = list_clouds(auth_context.owner)
    if not auth_context.is_owner():
        clouds = [cloud for cloud in clouds if cloud['id'] in
                  auth_context.get_allowed_resources(rtype='clouds')]
    return clouds


def list_clouds(owner):
    return [cloud.as_dict() for cloud in Cloud.objects(owner=owner,
                                                       deleted=None)]
