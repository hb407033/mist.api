import os
import re
import uuid
import json
import logging
from time import time

import paramiko

from libcloud.compute.types import NodeState
from libcloud.container.base import Container

from base64 import b64encode

from memcache import Client as MemcacheClient

from celery import group
from celery import Celery, Task
from celery.exceptions import SoftTimeLimitExceeded

from paramiko.ssh_exception import SSHException

from mist.api.exceptions import MistError, NotFoundError
from mist.api.exceptions import ServiceUnavailableError, MachineNotFoundError
from mist.api.shell import Shell

from mist.api.users.models import User, Owner, Organization
from mist.api.clouds.models import Cloud, DockerCloud
from mist.api.machines.models import Machine
from mist.api.scripts.models import Script
from mist.api.schedules.models import Schedule
from mist.api.dns.models import Zone, Record, RECORDS

from mist.api.poller.models import ListMachinesPollingSchedule
from mist.api.poller.models import PingProbeMachinePollingSchedule
from mist.api.poller.models import SSHProbeMachinePollingSchedule

celery_cfg = 'mist.core.celery_config'

from mist.api.helpers import send_email as helper_send_email
from mist.api.helpers import amqp_publish_user
from mist.api.helpers import amqp_owner_listening
from mist.api.helpers import amqp_log
from mist.api.helpers import trigger_session_update

from mist.api.logs.methods import log_event

from mist.api import config

logging.basicConfig(level=config.PY_LOG_LEVEL,
                    format=config.PY_LOG_FORMAT,
                    datefmt=config.PY_LOG_FORMAT_DATE)
log = logging.getLogger(__name__)

app = Celery('tasks')
app.conf.update(**config.CELERY_SETTINGS)
app.autodiscover_tasks(['mist.api.poller'])
app.autodiscover_tasks(['mist.api.portal'])


@app.task
def ssh_command(owner_id, cloud_id, machine_id, host, command,
                key_id=None, username=None, password=None, port=22):

    owner = Owner.objects.get(id=owner_id)
    shell = Shell(host)
    key_id, ssh_user = shell.autoconfigure(owner, cloud_id, machine_id,
                                           key_id, username, password, port)
    retval, output = shell.command(command)
    shell.disconnect()
    if retval:
        from mist.api.methods import notify_user
        notify_user(owner, "Async command failed for machine %s (%s)" %
                    (machine_id, host), output)


@app.task(bind=True, default_retry_delay=3*60)
def post_deploy_steps(self, owner_id, cloud_id, machine_id, monitoring,
                      key_id=None, username=None, password=None, port=22,
                      script_id='', script_params='', job_id=None, job=None,
                      hostname='', plugins=None, script='',
                      post_script_id='', post_script_params='', schedule={}):
    #TODO: break into subtasks

    from mist.api.methods import connect_provider, probe_ssh_only
    from mist.api.methods import notify_user, notify_admin

    try:
        from mist.core.methods import enable_monitoring
    except ImportError:
        from mist.api.dummy.methods import enable_monitoring

    job_id = job_id or uuid.uuid4().hex
    owner = Owner.objects.get(id=owner_id)
    tmp_log = lambda msg, *args: log.error('Post deploy: %s' % msg, *args)
    tmp_log('Entering post deploy steps for %s %s %s',
            owner.id, cloud_id, machine_id)

    try:
        # find the node we're looking for and get its hostname
        node = None
        try:
            cloud = Cloud.objects.get(owner=owner, id=cloud_id, deleted=None)
            conn = connect_provider(cloud)

            if isinstance(cloud, DockerCloud):
                nodes = conn.list_containers()
            else:
                nodes = conn.list_nodes()  # TODO: use cache
            for n in nodes:
                if n.id == machine_id:
                    node = n
                    break
            tmp_log('run list_machines')
        except:
            raise self.retry(exc=Exception(), countdown=10, max_retries=10)

        if node and isinstance(node, Container):
            node = cloud.ctl.compute.inspect_node(node)

        if node and len(node.public_ips):
            # filter out IPv6 addresses
            ips = filter(lambda ip: ':' not in ip, node.public_ips)
            host = ips[0]
        else:
            tmp_log('ip not found, retrying')
            raise self.retry(exc=Exception(), countdown=60, max_retries=20)

        if node.state != NodeState.RUNNING:
            tmp_log('not running state')
            raise self.retry(exc=Exception(), countdown=120, max_retries=30)

        machine = Machine.objects.get(cloud=cloud, machine_id=machine_id,
                                      state__ne='terminated')

        if schedule and schedule.get('name'): # ugly hack to prevent dupes
            log_dict = {
                'owner_id': owner.id,
                'event_type': 'job',
                'cloud_id': cloud_id,
                'machine_id': machine_id,
                'job_id': job_id,
                'job': job,
                'host': host,
                'key_id': key_id,
            }
            try:
                from mist.core.rbac.methods import AuthContext
            except ImportError:
                from mist.api.dummy.rbac import AuthContext

            try:
                name = schedule.get('action') + '-' + schedule.pop('name') + '-' + machine_id[:4]

                auth_context = AuthContext.deserialize(
                    schedule.pop('auth_context'))
                tmp_log('Add scheduler entry %s', name)
                schedule['conditions'] = [{
                    'type': 'machines',
                    'ids': [machine.id]
                }]
                schedule_info = Schedule.add(auth_context, name, **schedule)
                tmp_log("A new scheduler was added")
                log_event(action='Add scheduler entry',
                          scheduler=schedule_info.as_dict(), **log_dict)
            except Exception as e:
                print repr(e)
                error = repr(e)
                notify_user(owner, "add scheduler entry failed for "
                                   "machine %s" % machine_id, repr(e),
                            error=error)
                log_event(action='Add scheduler entry failed',
                          error=error, **log_dict)

        try:
            from mist.api.shell import Shell
            shell = Shell(host)
            # connect with ssh even if no command, to create association
            # to be able to enable monitoring
            tmp_log('attempting to connect to shell')
            key_id, ssh_user = shell.autoconfigure(
                owner, cloud_id, node.id, key_id, username, password, port
            )
            tmp_log('connected to shell')
            result = probe_ssh_only(owner, cloud_id, machine_id, host=None,
                                    key_id=key_id, ssh_user=ssh_user,
                                    shell=shell)
            log_dict = {
                'owner_id': owner.id,
                'event_type': 'job',
                'cloud_id': cloud_id,
                'machine_id': machine_id,
                'job_id': job_id,
                'job': job,
                'host': host,
                'key_id': key_id,
                'ssh_user': ssh_user,
                }
            log_event(action='probe', result=result, **log_dict)
            cloud = Cloud.objects.get(owner=owner, id=cloud_id, deleted=None)
            msg = "Cloud:\n  Name: %s\n  Id: %s\n" % (cloud.title, cloud_id)
            msg += "Machine:\n  Name: %s\n  Id: %s\n" % (node.name, node.id)

            if hostname:
                try:
                    kwargs = {}
                    kwargs['name'] = hostname
                    kwargs['type'] = 'A'
                    kwargs['data'] = host
                    kwargs['ttl'] = 3600

                    dns_cls = RECORDS[kwargs['type']]
                    record = dns_cls.add(owner=owner, **kwargs)
                    log_event(action='Create_A_record', hostname=hostname,
                              **log_dict)
                except Exception as exc:
                    log_event(action='Create_A_record', hostname=hostname,
                              error=str(exc), **log_dict)

            error = False
            if script_id:
                tmp_log('will run script_id %s', script_id)
                ret = run_script.run(
                    owner, script_id, machine.id,
                    params=script_params, host=host, job_id=job_id
                )
                error = ret['error']
                tmp_log('executed script_id %s', script_id)
            elif script:
                tmp_log('will run script')
                log_event(action='deployment_script_started', command=script,
                          **log_dict)
                start_time = time()
                retval, output = shell.command(script)
                tmp_log('executed script %s', script)
                execution_time = time() - start_time
                output = output.decode('utf-8', 'ignore')
                title = "Deployment script %s" % ('failed' if retval
                                                  else 'succeeded')
                error = retval > 0
                notify_user(owner, title,
                            cloud_id=cloud_id,
                            machine_id=machine_id,
                            machine_name=node.name,
                            command=script,
                            output=output,
                            duration=execution_time,
                            retval=retval,
                            error=retval > 0)
                log_event(action='deployment_script_finished',
                          error=retval > 0,
                          return_value=retval,
                          command=script,
                          stdout=output,
                          **log_dict)

            shell.disconnect()

            if monitoring:
                try:
                    enable_monitoring(
                        owner, cloud_id, node.id,
                        name=node.name, dns_name=node.extra.get('dns_name', ''),
                        public_ips=ips, no_ssh=False, dry=False, job_id=job_id,
                        plugins=plugins, deploy_async=False,
                    )
                except Exception as e:
                    print repr(e)
                    error = True
                    notify_user(owner, "Enable monitoring failed for machine %s"
                                % machine_id, repr(e))
                    notify_admin('Enable monitoring on creation failed for '
                                 'user %s machine %s: %r'
                                 % (str(owner), machine_id, e))
                    log_event(action='enable_monitoring_failed', error=repr(e),
                              **log_dict)

            if post_script_id:
                tmp_log('will run post_script_id %s', post_script_id)
                ret = run_script.run(
                    owner, post_script_id, machine.id,
                    params=post_script_params, host=host, job_id=job_id,
                    action_prefix='post_',
                )
                error = ret['error']
                tmp_log('executed post_script_id %s', post_script_id)

            log_event(action='post_deploy_finished', error=error, **log_dict)

        except (ServiceUnavailableError, SSHException) as exc:
            tmp_log(repr(exc))
            raise self.retry(exc=exc, countdown=60, max_retries=15)
    except Exception as exc:
        tmp_log(repr(exc))
        if str(exc).startswith('Retry'):
            raise
        notify_user(owner, "Deployment script failed for machine %s" % machine_id)
        notify_admin("Deployment script failed for machine %s in cloud %s by "
                     "user %s" % (machine_id, cloud_id, str(owner)), repr(exc))
        log_event(
            owner.id,
            event_type='job',
            action='post_deploy_finished',
            cloud_id=cloud_id,
            machine_id=machine_id,
            enable_monitoring=bool(monitoring),
            command=script,
            error="Couldn't connect to run post deploy steps.",
            job_id=job_id,
            job=job
        )


@app.task(bind=True, default_retry_delay=2*60)
def openstack_post_create_steps(self, owner_id, cloud_id, machine_id,
                                monitoring, key_id, username, password,
                                public_key, script='',
                                script_id='', script_params='', job_id=None,
                                job=None, hostname='', plugins=None,
                                post_script_id='', post_script_params='',
                                networks=[], schedule={}):

    from mist.api.methods import connect_provider
    owner = Owner.objects.get(id=owner_id)

    try:
        cloud = Cloud.objects.get(owner=owner, id=cloud_id, deleted=None)
        conn = connect_provider(cloud)
        nodes = conn.list_nodes()
        node = None

        for n in nodes:
            if n.id == machine_id:
                node = n
                break

        if node and node.state == 0 and len(node.public_ips):
            # filter out IPv6 addresses
            ips = filter(lambda ip: ':' not in ip, node.public_ips)
            host = ips[0]

            post_deploy_steps.delay(
                owner.id, cloud_id, machine_id, monitoring, key_id,
                script=script, script_id=script_id, script_params=script_params,
                job_id=job_id, job=job, hostname=hostname, plugins=plugins,
                post_script_id=post_script_id,
                post_script_params=post_script_params, schedule=schedule
            )

        else:
            try:
                created_floating_ips = []
                for network in networks['public']:
                    created_floating_ips += [floating_ip for floating_ip
                                             in network['floating_ips']]

                # From the already created floating ips try to find one
                # that is not associated to a node
                unassociated_floating_ip = None
                for ip in created_floating_ips:
                    if not ip['node_id']:
                        unassociated_floating_ip = ip
                        break

                # Find the ports which are associated to the machine
                # (e.g. the ports of the private ips)
                # and use one to associate a floating ip
                ports = conn.ex_list_ports()
                machine_port_id = None
                for port in ports:
                    if port.get('device_id') == node.id:
                        machine_port_id = port.get('id')
                        break

                if unassociated_floating_ip:
                    log.info("Associating floating "
                             "ip with machine: %s" % node.id)
                    ip = conn.ex_associate_floating_ip_to_node(
                        unassociated_floating_ip['id'], machine_port_id)
                else:
                    # Find the external network
                    log.info("Create and associating floating ip with "
                             "machine: %s" % node.id)
                    ext_net_id = networks['public'][0]['id']
                    ip = conn.ex_create_floating_ip(ext_net_id, machine_port_id)

                post_deploy_steps.delay(
                    owner.id, cloud_id, machine_id, monitoring, key_id,
                    script=script,
                    script_id=script_id, script_params=script_params,
                    job_id=job_id, job=job, hostname=hostname, plugins=plugins,
                    post_script_id=post_script_id,
                    post_script_params=post_script_params,
                )

            except:
                raise self.retry(exc=Exception(), max_retries=20)
    except Exception as exc:
        if str(exc).startswith('Retry'):
            raise


@app.task(bind=True, default_retry_delay=2*60)
def azure_post_create_steps(self, owner_id, cloud_id, machine_id, monitoring,
                            key_id, username, password, public_key, script='',
                            script_id='', script_params='', job_id=None,
                            job=None, hostname='', plugins=None,
                            post_script_id='', post_script_params='',
                            schedule={}):
    from mist.api.methods import connect_provider

    owner = Owner.objects.get(id=owner_id)
    try:
        # find the node we're looking for and get its hostname
        cloud = Cloud.objects.get(owner=owner, id=cloud_id, deleted=None)
        conn = connect_provider(cloud)
        nodes = conn.list_nodes()
        node = None
        for n in nodes:
            if n.id == machine_id:
                node = n
                break
        if node and node.state == NodeState.RUNNING and len(node.public_ips):
            # filter out IPv6 addresses
            ips = filter(lambda ip: ':' not in ip, node.public_ips)
            host = ips[0]
        else:
            raise self.retry(exc=Exception(), max_retries=20)

        try:
            # login with user, password. Deploy the public key, enable sudo
            # access for username, disable password authentication
            # and reload ssh.
            # After this is done, call post_deploy_steps if deploy script
            # or monitoring is provided
            ssh = paramiko.SSHClient()
            ssh.load_system_host_keys()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, username=username, password=password,
                        timeout=None, allow_agent=False, look_for_keys=False)

            ssh.exec_command('mkdir -p ~/.ssh && echo "%s" >> ~/.ssh/authorized_keys && chmod -R 700 ~/.ssh/' % public_key)

            chan = ssh.get_transport().open_session()
            chan.get_pty()
            chan.exec_command('sudo su -c \'echo "%s ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers\' ' % username)
            chan.send('%s\n' % password)

            check_sudo_command = 'sudo su -c \'whoami\''

            chan = ssh.get_transport().open_session()
            chan.get_pty()
            chan.exec_command(check_sudo_command)
            output = chan.recv(1024)

            if not output.startswith('root'):
                raise
            cmd = 'sudo su -c \'sed -i "s|[#]*PasswordAuthentication yes|PasswordAuthentication no|g" /etc/ssh/sshd_config &&  /etc/init.d/ssh reload; service ssh reload\' '
            ssh.exec_command(cmd)

            ssh.close()

            post_deploy_steps.delay(
                owner.id, cloud_id, machine_id, monitoring, key_id,
                script=script,
                script_id=script_id, script_params=script_params,
                job_id=job_id, job=job, hostname=hostname, plugins=plugins,
                post_script_id=post_script_id,
                post_script_params=post_script_params, schedule=schedule,
            )

        except Exception as exc:
            raise self.retry(exc=exc, countdown=10, max_retries=15)
    except Exception as exc:
        if str(exc).startswith('Retry'):
            raise


@app.task(bind=True, default_retry_delay=2*60)
def rackspace_first_gen_post_create_steps(
        self, owner_id, cloud_id, machine_id, monitoring, key_id, password,
        public_key, username='root', script='', script_id='', script_params='',
        job_id=None, job=None, hostname='', plugins=None, post_script_id='',
        post_script_params='', schedule={}):
    from mist.api.methods import connect_provider

    owner = Owner.objects.get(id=owner_id)
    try:
        # find the node we're looking for and get its hostname
        cloud = Cloud.objects.get(owner=owner, id=cloud_id, deleted=None)
        conn = connect_provider(cloud)
        nodes = conn.list_nodes()
        node = None
        for n in nodes:
            if n.id == machine_id:
                node = n
                break

        if node and node.state == 0 and len(node.public_ips):
            # filter out IPv6 addresses
            ips = filter(lambda ip: ':' not in ip, node.public_ips)
            host = ips[0]
        else:
            raise self.retry(exc=Exception(), max_retries=20)

        try:
            # login with user, password and deploy the ssh public key.
            # Disable password authentication and reload ssh.
            # After this is done, call post_deploy_steps
            # if deploy script or monitoring
            # is provided
            ssh = paramiko.SSHClient()
            ssh.load_system_host_keys()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, username=username, password=password, timeout=None, allow_agent=False, look_for_keys=False)

            ssh.exec_command('mkdir -p ~/.ssh && echo "%s" >> ~/.ssh/authorized_keys && chmod -R 700 ~/.ssh/' % public_key)

            cmd = 'sudo su -c \'sed -i "s|[#]*PasswordAuthentication yes|PasswordAuthentication no|g" /etc/ssh/sshd_config &&  /etc/init.d/ssh reload; service ssh reload\' '
            ssh.exec_command(cmd)

            ssh.close()

            post_deploy_steps.delay(
                owner.id, cloud_id, machine_id, monitoring, key_id,
                script=script,
                script_id=script_id, script_params=script_params,
                job_id=job_id, job=job, hostname=hostname, plugins=plugins,
                post_script_id=post_script_id,
                post_script_params=post_script_params, schedule=schedule
            )

        except Exception as exc:
            raise self.retry(exc=exc, countdown=10, max_retries=15)
    except Exception as exc:
        if str(exc).startswith('Retry'):
            raise


class UserTask(Task):
    abstract = True
    task_key = ''
    result_expires = 0
    result_fresh = 0
    polling = False
    _ut_cache = None

    @property
    def memcache(self):
        if self._ut_cache is None:
            self._ut_cache = MemcacheClient(config.MEMCACHED_HOST)
        return self._ut_cache

    def smart_delay(self, *args, **kwargs):
        """Return cached result if it exists, send job to celery if needed"""
        # check cache
        id_str = json.dumps([self.task_key, args, kwargs])
        cache_key = b64encode(id_str)
        cached = self.memcache.get(cache_key)
        if cached:
            age = time() - cached['timestamp']
            if age > self.result_fresh:
                amqp_log("%s: scheduling task" % id_str)
                if kwargs.pop('blocking', None):
                    return self.execute(*args, **kwargs)
                else:
                    self.delay(*args, **kwargs)
            if age < self.result_expires:
                amqp_log("%s: smart delay cache hit" % id_str)
                return cached['payload']
        else:
            if kwargs.pop('blocking', None):
                return self.execute(*args, **kwargs)
            else:
                self.delay(*args, **kwargs)

    def clear_cache(self, *args, **kwargs):
        id_str = json.dumps([self.task_key, args, kwargs])
        cache_key = b64encode(id_str)
        log.info("Clearing cache for '%s'", id_str)
        return self.memcache.delete(cache_key)

    def run(self, *args, **kwargs):
        owner_id = args[0]
        if '@' in owner_id:
            owner_id = User.objects.get(email=owner_id).id
            args[0] = owner_id
        log.error('Running %s for %s', self.__class__.__name__, owner_id)
        # seq_id is an id for the sequence of periodic tasks, to avoid
        # running multiple concurrent sequences of the same task with the
        # same arguments. it is empty on first run, constant afterwards
        seq_id = kwargs.pop('seq_id', '')
        id_str = json.dumps([self.task_key, args, kwargs])
        cache_key = b64encode(id_str)
        cached_err = self.memcache.get(cache_key + 'error')
        if cached_err:
            # task has been failing recently
            if seq_id != cached_err['seq_id']:
                if seq_id:
                    # other sequence of tasks has taken over
                    return
                else:
                    # taking over from other sequence
                    cached_err = None
                    # cached err will be deleted or overwritten in a while
                    #self.memcache.delete(cache_key + 'error')
        if not amqp_owner_listening(owner_id):
            # noone is waiting for result, stop trying, but flush cached erros
            self.memcache.delete(cache_key + 'error')
            return
        # check cache to stop iteration if other sequence has started
        cached = self.memcache.get(cache_key)
        if cached:
            if seq_id and seq_id != cached['seq_id']:
                amqp_log("%s: found new cached seq_id [%s], "
                         "stopping iteration of [%s]" % (id_str,
                                                         cached['seq_id'],
                                                         seq_id))
                return
            elif not seq_id and time() - cached['timestamp'] < self.result_fresh:
                amqp_log("%s: fresh task submitted with fresh cached result "
                         ", dropping" % id_str)
                return
        if not seq_id:
            # this task is called externally, not a rerun, create a seq_id
            amqp_log("%s: fresh task submitted [%s]" % (id_str, seq_id))
            seq_id = uuid.uuid4().hex
        # actually run the task
        try:
            data = self.execute(*args, **kwargs)
        except Exception as exc:
            # error handling
            if isinstance(exc, SoftTimeLimitExceeded):
                log.error("SoftTimeLimitExceeded: %s", id_str)
            now = time()
            if not cached_err:
                cached_err = {'seq_id': seq_id, 'timestamps': []}
            cached_err['timestamps'].append(now)
            x0 = cached_err['timestamps'][0]
            rel_points = [x - x0 for x in cached_err['timestamps']]
            rerun = self.error_rerun_handler(exc, rel_points, *args, **kwargs)
            if rerun is not None:
                self.memcache.set(cache_key + 'error', cached_err)
                kwargs['seq_id'] = seq_id
                self.apply_async(args, kwargs, countdown=rerun)
            else:
                self.memcache.delete(cache_key + 'error')
            amqp_log("%s: error %r, rerun %s" % (id_str, exc, rerun))
            return
        else:
            self.memcache.delete(cache_key + 'error')
        cached = {'timestamp': time(), 'payload': data, 'seq_id': seq_id}
        ok = amqp_publish_user(owner_id, routing_key=self.task_key, data=data)
        if not ok:
            # echange closed, no one gives a shit, stop repeating, why try?
            amqp_log("%s: exchange closed" % id_str)
            return
        kwargs['seq_id'] = seq_id
        self.memcache.set(cache_key, cached)
        if self.polling:
            amqp_log("%s: will rerun in %d secs [%s]" % (id_str,
                                                         self.result_fresh,
                                                         seq_id))
            self.apply_async(args, kwargs, countdown=self.result_fresh)

    def execute(self, *args, **kwargs):
        raise NotImplementedError()

    def error_rerun_handler(self, exc, errors, *args, **kwargs):
        """Accepts a list of relative time points of consecutive errors,
        returns number of seconds to retry in or None to stop retrying."""
        if len(errors) == 1:
            return 30  # Retry in 30sec after the first error
        if len(errors) == 2:
            return 120  # Retry in 120sec after the second error
        if len(errors) == 3:
            return 60 * 10  # Retry in 10mins after the third error


class ListSizes(UserTask):
    abstract = False
    task_key = 'list_sizes'
    result_expires = 60 * 60 * 24 * 7
    result_fresh = 60 * 60
    polling = False
    soft_time_limit = 30

    def execute(self, owner_id, cloud_id):
        from mist.api import methods
        owner = Owner.objects.get(id=owner_id)
        sizes = methods.list_sizes(owner, cloud_id)
        return {'cloud_id': cloud_id, 'sizes': sizes}


class ListLocations(UserTask):
    abstract = False
    task_key = 'list_locations'
    result_expires = 60 * 60 * 24 * 7
    result_fresh = 60 * 60
    polling = False
    soft_time_limit = 30

    def execute(self, owner_id, cloud_id):
        from mist.api import methods
        owner = Owner.objects.get(id=owner_id)
        locations = methods.list_locations(owner, cloud_id)
        return {'cloud_id': cloud_id, 'locations': locations}


class ListNetworks(UserTask):
    abstract = False
    task_key = 'list_networks'
    result_expires = 60 * 60 * 24
    result_fresh = 0
    polling = False
    soft_time_limit = 60

    def execute(self, owner_id, cloud_id):
        owner = Owner.objects.get(id=owner_id)
        log.warn('Running list networks for user %s cloud %s'
                 % (owner.id, cloud_id))
        from mist.api.networks.methods import list_networks
        networks = list_networks(owner, cloud_id)
        log.warn('Returning list networks for user %s cloud %s'
                 % (owner.id, cloud_id))
        return {'cloud_id': cloud_id, 'networks': networks}


class ListZones(UserTask):
    abstract = False
    task_key = 'list_zones'
    result_expires = 60 * 60 * 24
    result_fresh = 0
    polling = False
    soft_time_limit = 60

    def execute(self, owner_id, cloud_id):
        owner = Owner.objects.get(id=owner_id)
        log.warn('Running list zones for user %s cloud %s'
                 % (owner.id, cloud_id))
        from mist.api.dns.methods import list_zones
        try:
            cloud = Cloud.objects.get(owner=owner, id=cloud_id)
        except Cloud.DoesNotExist:
            raise CloudNotFoundError
        if not hasattr(cloud.ctl, 'dns'):
            return {'cloud_id': cloud_id, 'zones': []}
        ret = []
        if cloud.dns_enabled:
            ret = list_zones(owner, cloud.id)
            log.warn('Returning list zones for user %s cloud %s'
                     % (owner.id, cloud_id))
        return {'cloud_id': cloud_id, 'zones': ret}


class ListImages(UserTask):
    abstract = False
    task_key = 'list_images'
    result_expires = 60 * 60 * 24 * 7
    result_fresh = 60 * 60
    polling = False
    soft_time_limit = 60*2

    def execute(self, owner_id, cloud_id):
        from mist.api import methods
        owner = Owner.objects.get(id=owner_id)
        log.warn('Running list images for user %s cloud %s',
                 owner.id, cloud_id)
        images = methods.list_images(owner, cloud_id)
        log.warn('Returning list images for user %s cloud %s',
                 owner.id, cloud_id)
        return {'cloud_id': cloud_id, 'images': images}


class ListProjects(UserTask):
    abstract = False
    task_key = 'list_projects'
    result_expires = 60 * 60 * 24 * 7
    result_fresh = 60 * 60
    polling = False
    soft_time_limit = 30

    def execute(self, owner_id, cloud_id):
        owner = Owner.objects.get(id=owner_id)
        log.warn('Running list projects for user %s cloud %s',
                 owner.id, cloud_id)
        from mist.api import methods
        projects = methods.list_projects(owner, cloud_id)
        log.warn('Returning list projects for user %s cloud %s',
                 owner.id, cloud_id)
        return {'cloud_id': cloud_id, 'projects': projects}


class ListResourceGroups(UserTask):
    abstract = False
    task_key = 'list_resource_groups'
    result_expires = 60 * 60 * 24 * 7
    result_fresh = 60 * 60
    polling = False
    soft_time_limit = 30

    def execute(self, owner_id, cloud_id):
        owner = Owner.objects.get(id=owner_id)
        log.warn('Running list resource groups for user %s cloud %s',
                 owner.id, cloud_id)
        from mist.api import methods
        resource_groups = methods.list_resource_groups(owner, cloud_id)
        log.warn('Returning list resource groups for user %s cloud %s',
                 owner.id, cloud_id)
        return {'cloud_id': cloud_id, 'resource_groups': resource_groups}

class ListStorageAccounts(UserTask):
    abstract = False
    task_key = 'list_storage_accounts'
    result_expires = 60 * 60 * 24 * 7
    result_fresh = 60 * 60
    polling = False
    soft_time_limit = 30

    def execute(self, owner_id, cloud_id):
        owner = Owner.objects.get(id=owner_id)
        log.warn('Running list storage accounts for user %s cloud %s',
                 owner.id, cloud_id)
        from mist.api import methods
        storage_accounts = methods.list_storage_accounts(owner, cloud_id)
        log.warn('Returning list storage accounts for user %s cloud %s',
                 owner.id, cloud_id)
        return {'cloud_id': cloud_id, 'storage_accounts': storage_accounts}


class ListMachines(UserTask):
    abstract = False
    task_key = 'list_machines'
    result_expires = 60 * 60 * 24
    result_fresh = 10
    polling = True
    soft_time_limit = 60

    def execute(self, owner_id, cloud_id):
        from mist.api.machines.methods import list_machines
        owner = Owner.objects.get(id=owner_id)
        log.warn('Running list machines for user %s cloud %s',
                 owner.id, cloud_id)
        machines = list_machines(owner, cloud_id)

        for machine in machines:
            # TODO tags tags tags
            if machine.get("tags"):
                tags = {}
                for tag in machine["tags"]:
                    tags[tag["key"]] = tag["value"]
            try:
                from mist.api.tag.methods import resolve_id_and_get_tags
                mistio_tags = resolve_id_and_get_tags(
                    owner,
                    'machine',
                    machine.get("machine_id"),
                    cloud_id=cloud_id
                )
            except:
                log.info("Machine has not tags in mist db")
                mistio_tags = {}
            else:
                machine["tags"] = []
                # optimized for js
                for tag in mistio_tags:
                    machine['tags'].append(tag)
            # FIXME: optimize!
        log.warn('Returning list machines for user %s cloud %s',
                 owner.id, cloud_id)
        return {'cloud_id': cloud_id, 'machines': machines}

    def error_rerun_handler(self, exc, errors, owner_id, cloud_id):
        from mist.api.methods import notify_user

        if len(errors) < 6:
            return self.result_fresh  # Retry when the result is no longer fresh
        owner = Owner.objects.get(id=owner_id)
        cloud = Cloud.objects.get(owner=owner, id=cloud_id, deleted=None)

        if len(errors) == 6:  # If does not respond for a minute
            notify_user(owner, 'Cloud %s does not respond' % cloud.title,
                        email_notify=False, cloud_id=cloud_id)

        # Keep retrying every 30 secs for 10 minutes, then every 60 secs for
        # 20 minutes and finally every 20 minutes
        times = [30]*20 + [60]*20
        index = len(errors) - 6
        if index < len(times):
            return times[index]
        else: #
            return 20*60


class ProbeSSH(UserTask):
    abstract = False
    task_key = 'probe'
    result_expires = 60 * 60 * 2
    result_fresh = 60 * 2
    polling = True
    soft_time_limit = 60

    def execute(self, owner_id, cloud_id, machine_id, host, machine_uuid):
        owner = Owner.objects.get(id=owner_id)
        from mist.api.methods import probe_ssh_only
        res = probe_ssh_only(owner, cloud_id, machine_id, host)
        return {'cloud_id': cloud_id,
                'machine_id': machine_id,
                'machine_uuid': machine_uuid,
                'host': host,
                'result': res}

    def error_rerun_handler(self, exc, errors, *args, **kwargs):
        # Retry in 2, 4, 8, 16, 32, 32, 32, 32, 32, 32 minutes
        t = 60 * 2 ** len(errors)
        return t if t < 60 * 32 else 60 * 32


class Ping(UserTask):
    abstract = False
    task_key = 'ping'
    result_expires = 60 * 60 * 2
    result_fresh = 60 * 15
    polling = True
    soft_time_limit = 30

    def execute(self, owner_id, cloud_id, machine_id, host):
        from mist.api import methods
        res = methods.ping(owner=Owner.objects.get(id=owner_id), host=host)
        return {'cloud_id': cloud_id,
                'machine_id': machine_id,
                'host': host,
                'result': res}

    def error_rerun_handler(self, exc, errors, *args, **kwargs):
        return self.result_fresh


@app.task
def deploy_collectd(owner_id, cloud_id, machine_id, extra_vars, job_id='',
                    job=None, plugins=None):
    # FIXME
    from mist.api.methods import deploy_collectd

    owner = Owner.objects.get(id=owner_id)
    cloud = Cloud.objects.get(owner=owner, id=cloud_id, deleted=None)
    machine = Machine.objects.get(cloud=cloud, machine_id=machine_id)
    machine.monitoring.installation_status.state = 'installing'
    machine.save()

    trigger_session_update(owner, ['monitoring'])

    log_dict = {
        'owner_id': owner.id,
        'event_type': 'job',
        'cloud_id': cloud_id,
        'machine_id': machine_id,
        'job_id': job_id or uuid.uuid4().hex,
        'job': job,
    }
    log_event(action='deploy_collectd_started', **log_dict)
    ret_dict = deploy_collectd(owner, cloud_id, machine_id, extra_vars)
    error = False if ret_dict['success'] else (ret_dict['error_msg'] or True)
    if plugins and not error:
        for script_id in plugins:
            try:
                script = Script.objects.get(owner=owner, id=script_id,
                                            deleted=None)
                ret = script.ctl.deploy_and_assoc_python_plugin_from_script(
                    machine)
            except Exception as exc:
                log_event(
                    action='deploy_collectd_python_plugin',
                    plugin_script_id=script_id, error=str(exc), **log_dict
                )
                if not error:
                    error = "Deployment of '%s' plugin failed." % script_id
            else:
                log_event(
                    action='deploy_collectd_python_plugin',
                    plugin_script_id=script_id, metric_id=ret['metric_id'],
                    stdout=ret['stdout'], **log_dict
                )

    log_event(action='deploy_collectd_finished', error=error,
              stdout=ret_dict['stdout'], **log_dict)

    if ret_dict['success']:
        machine.monitoring.installation_status.state = 'succeeded'
    else:
        machine.monitoring.installation_status.state = 'failed'
    machine.monitoring.installation_status.finished_at = time()
    machine.monitoring.installation_status.stdout = ret_dict['stdout']
    machine.monitoring.installation_status.error_msg = ret_dict['error_msg']
    machine.save()
    trigger_session_update(owner, ['monitoring'])


@app.task
def undeploy_collectd(owner_id, cloud_id, machine_id):
    import mist.api.methods
    owner = Owner.objects.get(id=owner_id)
    mist.api.methods.undeploy_collectd(owner, cloud_id, machine_id)


@app.task
def create_machine_async(owner_id, cloud_id, key_id, machine_name, location_id,
                         image_id, size_id, image_extra, disk,
                         image_name, size_name, location_name, ips, monitoring,
                         ex_storage_account, machine_password, ex_resource_group,
                         networks, docker_env, docker_command, script='',
                         script_id='', script_params='',
                         post_script_id='', post_script_params='',
                         quantity=1, persist=False, job_id=None, job=None,
                         docker_port_bindings={}, docker_exposed_ports={},
                         azure_port_bindings='', hostname='', plugins=None,
                         disk_size=None, disk_path=None, create_storage_account=False,
                         new_storage_account='', create_resource_group=False,
                         new_resource_group='', create_network=False,
                         new_network='', cloud_init='', associate_floating_ip=False,
                         associate_floating_ip_subnet=None, project_id=None,
                         tags=None, schedule={}, bare_metal=False, hourly=True,
                         softlayer_backend_vlan_id=None, size_ram=256, size_cpu=1,
                         size_disk_primary=5, size_disk_swap=1, boot=True, build=True,
                         cpu_priority=1, cpu_sockets=1, cpu_threads=1, port_speed=0,
                         hypervisor_group_id=None, machine_username=''):
    from multiprocessing.dummy import Pool as ThreadPool
    from mist.api.machines.methods import create_machine
    from mist.api.exceptions import MachineCreationError
    log.warn('MULTICREATE ASYNC %d' % quantity)

    job_id = job_id or uuid.uuid4().hex
    owner = Owner.objects.get(id=owner_id)

    names = []
    if quantity == 1:
        names = [machine_name]
    else:
        names = []
        for i in range(1, quantity + 1):
            names.append('%s-%d' % (machine_name, i))

    log_event(owner.id, 'job', 'async_machine_creation_started',
              job_id=job_id, job=job,
              cloud_id=cloud_id, script=script, script_id=script_id,
              script_params=script_params, monitoring=monitoring,
              persist=persist, quantity=quantity, key_id=key_id,
              machine_names=names)

    THREAD_COUNT = 5
    pool = ThreadPool(THREAD_COUNT)
    specs = []
    for name in names:
        specs.append((
            (owner, cloud_id, key_id, name, location_id, image_id,
             size_id, image_extra, disk, image_name, size_name,
             location_name, ips, monitoring, ex_storage_account,
             machine_password, ex_resource_group,networks, docker_env,
             docker_command, 22, script, script_id, script_params,
             job_id, job),
            {'hostname': hostname, 'plugins': plugins,
             'post_script_id': post_script_id,
             'post_script_params': post_script_params,
             'azure_port_bindings': azure_port_bindings,
             'associate_floating_ip': associate_floating_ip,
             'cloud_init': cloud_init,
             'disk_size': disk_size,
             'disk_path': disk_path,
             'project_id': project_id,
             'tags': tags,
             'schedule': schedule,
             'softlayer_backend_vlan_id': softlayer_backend_vlan_id,
             'size_ram': size_ram,
             'size_cpu': size_cpu,
             'size_disk_primary': size_disk_primary,
             'size_disk_swap': size_disk_swap,
             'create_network': create_network,
             'new_network': new_network,
             'create_resource_group': create_resource_group,
             'new_resource_group': new_resource_group,
             'create_storage_account': create_storage_account,
             'new_storage_account': new_storage_account,
             'boot': boot,
             'build': build,
             'bare_metal': bare_metal,
             'hourly': hourly,
             'cpu_priority': cpu_priority,
             'cpu_sockets': cpu_sockets,
             'cpu_threads': cpu_threads,
             'port_speed': port_speed,
             'hypervisor_group_id': hypervisor_group_id,
             'machine_username': machine_username}
        ))

    def create_machine_wrapper(args_kwargs):
        args, kwargs = args_kwargs
        error = False
        node = {}
        try:
            node = create_machine(*args, **kwargs)
        except MachineCreationError as exc:
            error = str(exc)
        except Exception as exc:
            error = repr(exc)
        finally:
            name = args[3]
            log_event(owner.id, 'job', 'machine_creation_finished', job=job,
                      job_id=job_id, cloud_id=cloud_id, machine_name=name,
                      error=error, machine_id=node.get('id', ''))

    pool.map(create_machine_wrapper, specs)
    pool.close()
    pool.join()


@app.task(bind=True, default_retry_delay=5, max_retries=3)
def send_email(self, subject, body, recipients, sender=None, bcc=None):
    if not helper_send_email(subject, body, recipients,
                             sender=sender, bcc=bcc, attempts=1):
        raise self.retry()
    return True


@app.task
def group_machines_actions(owner_id, action, name, machines_uuids):
    """
    Accepts a list of lists in form  cloud_id,machine_id and pass them
    to run_machine_action like a group

    :param owner_id:
    :param action:
    :param name:
    :param machines_uuids:
    :return: glist
    """
    glist = []

    for machine_uuid in machines_uuids:
        glist.append(run_machine_action.s(owner_id, action, name,
                                          machine_uuid))

    schedule = Schedule.objects.get(owner=owner_id, name=name, deleted=None)

    log_dict = {
        'schedule_id': schedule.id,
        'schedule_name': schedule.name,
        'description': schedule.description or '',
        'schedule_type': unicode(schedule.schedule_type or ''),
        'owner_id': owner_id,
        'machines_match': schedule.get_ids(),
        'machine_action': action,
        'expires': str(schedule.expires or ''),
        'task_enabled': schedule.task_enabled,
        'run_immediately': schedule.run_immediately,
        'event_type': 'job',
        'error': False,
    }

    log_event(action='Schedule started', **log_dict)
    log.info('Schedule action started: %s', log_dict)
    try:
        group(glist)()
    except Exception as exc:
        log_dict['error'] = str(exc)

    log_dict.update({'last_run_at': str(schedule.last_run_at or ''),
                    'total_run_count': schedule.total_run_count or 0,
                     'error': log_dict['error']}
                    )
    log_event(action='Schedule finished', **log_dict)
    if log_dict['error']:
        log.info('Schedule action failed: %s', log_dict)
    else:
        log.info('Schedule action succeeded: %s', log_dict)
    owner = Owner.objects.get(id=owner_id)
    trigger_session_update(owner, ['schedules'])
    return log_dict


@app.task(soft_time_limit=3600, time_limit=3630)
def run_machine_action(owner_id, action, name, machine_uuid):
    """
    Calls specific action for a machine and log the info
    :param owner_id:
    :param action:
    :param name:
    :param cloud_id:
    :param machine_id:
    :return:
    """
    schedule_id = Schedule.objects.get(owner=owner_id,
                                       name=name, deleted=None).id

    log_dict = {
        'owner_id': owner_id,
        'event_type': 'job',
        'machine_uuid': machine_uuid,
        'schedule_id': schedule_id,
    }

    machine_id = ''
    cloud_id = ''
    owner = Owner.objects.get(id=owner_id)
    started_at = time()
    try:
        machine = Machine.objects.get(id=machine_uuid, state__ne='terminated')
        cloud_id = machine.cloud.id
        machine_id = machine.machine_id
        log_dict.update({'cloud_id': cloud_id,
                         'machine_id': machine_id})
    except NotFoundError:
        log_dict['error'] = "Resource with that id does not exist."
        msg = action + ' failed'
        log_event(action=msg, **log_dict)
    except Exception as exc:
        log_dict['error'] = str(exc)
        msg = action + ' failed'
        log_event(action=msg, **log_dict)

    if not log_dict.get('error'):
        if action in ('start', 'stop', 'reboot', 'destroy'):
            # call list machines here cause we don't have another way
            # to update machine state if user isn't logged in
            from mist.api.machines.methods import list_machines, destroy_machine
            list_machines(owner, cloud_id) # TODO change this to
            # compute.ctl.list_machines

            if action == 'start':
                log_event(action='Start', **log_dict)
                try:
                    machine.ctl.start()
                except Exception as exc:
                    log_dict['error'] = str(exc) + \
                                        ' Machine in %s state' % machine.state
                    log_event(action='Start failed', **log_dict)
                else:
                    log_event(action='Start succeeded', **log_dict)
            elif action == 'stop':
                log_event(action='Stop', **log_dict)
                try:
                    machine.ctl.stop()
                except Exception as exc:
                    log_dict['error'] = str(exc) + \
                                        ' Machine in %s state' % machine.state
                    log_event(action='Stop failed', **log_dict)
                else:
                    log_event(action='Stop succeeded', **log_dict)
            elif action == 'reboot':
                log_event(action='Reboot', **log_dict)
                try:
                    machine.ctl.reboot()
                except Exception as exc:
                    log_dict['error'] = str(exc) + \
                                        ' Machine in %s state' % machine.state
                    log_event(action='Reboot failed', **log_dict)
                else:
                    log_event(action='Reboot succeeded', **log_dict)
            elif action == 'destroy':
                log_event(action='Destroy', **log_dict)
                try:
                    destroy_machine(owner, cloud_id, machine_id)
                except Exception as exc:
                    log_dict['error'] = str(exc) + \
                                        ' Machine in %s state' % machine.state
                    log_event(action='Destroy failed', **log_dict)
                else:
                    log_event(action='Destroy succeeded', **log_dict)
    # TODO markos asked this
    log_dict['started_at'] = started_at
    log_dict['finished_at'] = time()
    title = "Execution of '%s' action " % action
    title += "failed" if log_dict.get('error') else "succeeded"
    from mist.api.methods import notify_user
    notify_user(
        owner, title,
        cloud_id=cloud_id,
        machine_id=machine_id,
        duration=log_dict['finished_at'] - log_dict['started_at'],
        error=log_dict.get('error'),
    )


@app.task
def group_run_script(owner_id, script_id, name, machines_uuids):
    """
    Accepts a list of lists in form  cloud_id,machine_id and pass them
    to run_machine_action like a group

    :param owner_id:
    :param script_id:
    :param name
    :param cloud_machines_pairs:
    :return:
    """
    glist = []
    job_id = uuid.uuid4().hex
    for machine_uuid in machines_uuids:
            glist.append(run_script.s(owner_id, script_id, machine_uuid,
                                      job_id=job_id, job='schedule'))

    schedule = Schedule.objects.get(owner=owner_id, name=name, deleted=None)

    log_dict = {
        'schedule_id': schedule.id,
        'schedule_name': schedule.name,
        'description': schedule.description or '',
        'schedule_type': unicode(schedule.schedule_type or ''),
        'owner_id': owner_id,
        'machines_match': schedule.get_ids(),
        'script_id': script_id,
        'expires': str(schedule.expires or ''),
        'task_enabled': schedule.task_enabled,
        'run_immediately': schedule.run_immediately,
        'event_type': 'job',
        'error': False,
        'job': 'schedule',
        'job_id': job_id,
    }

    log_event(action='Schedule started', **log_dict)
    log.info('Schedule started: %s', log_dict )
    try:
        group(glist)()
    except Exception as exc:
        log_dict['error'] = str(exc)

    log_dict.update({'last_run_at': str(schedule.last_run_at or ''),
                     'total_run_count': schedule.total_run_count or 0,
                     'error': log_dict['error']}
                    )
    log_event(action='Schedule finished', **log_dict)
    if log_dict['error']:
        log.info('Schedule run_script failed: %s', log_dict)
    else:
        log.info('Schedule run_script succeeded: %s', log_dict)
    owner = Owner.objects.get(id=owner_id)
    trigger_session_update(owner, ['schedules'])
    return log_dict


@app.task(soft_time_limit=3600, time_limit=3630)
def run_script(owner, script_id, machine_uuid, params='', host='',
               key_id='', username='', password='', port=22, job_id='', job='',
               action_prefix='', su=False, env=""):
    import mist.api.shell
    from mist.api.methods import notify_admin, notify_user
    from mist.api.machines.methods import list_machines

    if not isinstance(owner, Owner):
        owner = Owner.objects.get(id=owner)

    ret = {
        'owner_id': owner.id,
        'job_id': job_id or uuid.uuid4().hex,
        'job': job,
        'script_id': script_id,
        # 'cloud_id': cloud_id,
        # 'machine_id': machine.id,
        'machine_uuid': machine_uuid,
        'params': params,
        'env': env,
        'su': su,
        'host': host,
        'key_id': key_id,
        'ssh_user': username,
        'port': port,
        'command': '',
        'stdout': '',
        'exit_code': '',
        'wrapper_stdout': '',
        'extra_output': '',
        'error': False,
    }
    started_at = time()
    machine_name = ''
    cloud_id = ''
    machine_id=''

    try:
        machine = Machine.objects.get(id=machine_uuid, state__ne='terminated')
        cloud_id = machine.cloud.id
        machine_id = machine.machine_id
        ret.update({'cloud_id': cloud_id, 'machine_id': machine_id})
        # cloud = Cloud.objects.get(owner=owner, id=cloud_id, deleted=None)
        script = Script.objects.get(owner=owner, id=script_id, deleted=None)

        if not host:
            # FIXME machine.cloud.ctl.compute.list_machines()
            for machine in list_machines(owner, cloud_id):
                if machine['machine_id'] == machine_id:
                    ips = [ip for ip in machine['public_ips'] if ':' not in ip]
                    # get private IPs if no public IP is available
                    if not ips:
                        ips = [ip for ip in machine['private_ips'] if ':' not in ip]
                    if ips:
                        host = ips[0]
                        ret['host'] = host
                    machine_name = machine['name']
                    break
        if not host:
            raise MistError("No host provided and none could be discovered.")
        shell = mist.api.shell.Shell(host)
        ret['key_id'], ret['ssh_user'] = shell.autoconfigure(
            owner, cloud_id, machine_id, username, password, port
        )
        # FIXME wrap here script.run_script
        path, params, wparams = script.ctl.run_script(shell,
                                                      params=params,
                                                      job_id=ret.get('job_id'))

        with open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)
            )))),
            'run_script', 'run.py'
        )) as fobj:
            wscript = fobj.read()

        # check whether python exists

        exit_code, wstdout = shell.command("command -v python")

        if exit_code > 0:
            command = "/bin/bash %s %s" % (path, params)
        else:
            command = "python - %s << EOF\n%s\nEOF\n" % (wparams, wscript)
        if su:
            command = 'sudo ' + command
        ret['command'] = command
    except Exception as exc:
        ret['error'] = str(exc)
    log_event(event_type='job', action=action_prefix+'script_started', **ret)
    log.info('Script started: %s', ret)
    if not ret['error']:
        try:
            exit_code, wstdout = shell.command(command)
            shell.disconnect()
            wstdout = wstdout.encode('utf-8', 'ignore')
            wstdout = wstdout.replace('\r\n', '\n').replace('\r', '\n')
            ret['wrapper_stdout'] = wstdout
            ret['exit_code'] = exit_code
            ret['stdout'] = wstdout
            try:
                parts = re.findall(r'-----part-([^-]*)-([^-]*)-----\n(.*?)-----part-end-\2-----\n',
                                   wstdout, re.DOTALL)
                if parts:
                    randid = parts[0][1]
                    for part in parts:
                        if part[1] != randid:
                            raise Exception('Different rand ids')
                    for part in parts:
                        if part[0] == 'script':
                            ret['stdout'] = part[2]
                        elif part[0] == 'outfile':
                            ret['extra_output'] = part[2]
            except Exception as exc:
                pass
            if exit_code > 0:
                ret['error'] = 'Script exited with return code %s' % exit_code
        except SoftTimeLimitExceeded:
            ret['error'] = 'Script execution time limit exceeded'
        except Exception as exc:
            ret['error'] = str(exc)
    log_event(event_type='job', action=action_prefix+'script_finished', **ret)
    if ret['error']:
        log.info('Script failed: %s', ret)
    else:
        log.info('Script succeeded: %s', ret)
    ret['started_at'] = started_at
    ret['finished_at'] = time()
    title = "Execution of '%s' script " % script.name
    title += "failed" if ret['error'] else "succeeded"
    notify_user(
        owner, title,
        cloud_id=cloud_id,
        machine_id=machine_id,
        machine_name=machine_name,
        output=ret['stdout'],
        duration=ret['finished_at'] - ret['started_at'],
        retval=ret['exit_code'],
        error=ret['error'],
    )
    if ret['error']:
        title += " for user %s" % str(owner)
        notify_admin(
            title, "%s\n\n%s" % (ret['stdout'], ret['error']), team = 'dev'
        )
    return ret


@app.task
def revoke_token(token):
    from mist.api.auth.models import AuthToken
    auth_token = AuthToken.objects.get(token=token)
    auth_token.invalidate()
    auth_token.save()


@app.task
def update_poller(org_id):
    org = Organization.objects.get(id=org_id)
    log.info("Updating poller for %s", org)
    for cloud in Cloud.objects(owner=org, deleted=None, enabled=True):
        log.info("Updating poller for cloud %s", cloud)
        ListMachinesPollingSchedule.add(cloud=cloud, interval=10, ttl=120)
        for machine in cloud.ctl.compute.list_cached_machines():
            log.info("Updating poller for machine %s", machine)
            PingProbeMachinePollingSchedule.add(machine=machine,
                                                interval=90, ttl=120)
            SSHProbeMachinePollingSchedule.add(machine=machine,
                                               interval=90, ttl=120)
