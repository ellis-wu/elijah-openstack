# Elijah: Cloudlet Infrastructure for Mobile Computing
#
#   Author: Kiryong Ha <krha@cmu.edu>
#
#   Copyright (C) 2011-2014 Carnegie Mellon University
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#

import eventlet
import threading
from urlparse import urlparse
from urlparse import urlsplit
import httplib

from nova.i18n import _LW
from nova import utils
from nova import exception
from nova import image as image
from nova.compute import api as nova_api
from nova.compute import rpcapi as nova_rpc
from nova.compute import vm_states
from nova.compute import task_states

from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils

from nova.objects import quotas as quotas_obj
from nova import objects
from hashlib import sha256

LOG = logging.getLogger(__name__)
CONF = cfg.CONF
CONF.import_opt('reclaim_instance_interval', 'nova.compute.cloudlet_manager')


class HandoffError(Exception):
    pass


class CloudletAPI(nova_rpc.ComputeAPI):

    PROPERTY_KEY_CLOUDLET = "is_cloudlet"
    PROPERTY_KEY_CLOUDLET_TYPE = "cloudlet_type"
    PROPERTY_KEY_NETWORK_INFO = "network"
    PROPERTY_KEY_BASE_UUID = "base_sha256_uuid"
    PROPERTY_KEY_BASE_RESOURCE = "base_resource_xml_str"

    IMAGE_TYPE_BASE_DISK = "cloudlet_base_disk"
    IMAGE_TYPE_BASE_MEM = "cloudlet_base_memory"
    IMAGE_TYPE_BASE_DISK_HASH = "cloudlet_base_disk_hash"
    IMAGE_TYPE_BASE_MEM_HASH = "cloudlet_base_memory_hash"
    IMAGE_TYPE_OVERLAY = "cloudlet_overlay"

    INSTANCE_TYPE_RESUMED_BASE = "cloudlet_resumed_base_instance"
    INSTANCE_TYPE_SYNTHESIZED_VM = "cloudlet_synthesized_vm"

    def __init__(self):
        # super(CloudletAPI, self).__init__(
        #        topic=CONF.compute_topic,
        #        default_version=CloudletAPI.BASE_RPC_API_VERSION)
        super(CloudletAPI, self).__init__()
        self.nova_api = nova_api.API()
        self.image_api = image.API()

    def _cloudlet_create_image(self, context, instance, name, image_type,
                               extra_properties=None):
        """Create new image entry in the image service.  This new image
        will be reserved for the compute manager to upload a snapshot
        or backup.

        :param context: security context
        :param instance: nova.db.sqlalchemy.models.Instance
        :param name: string for name of the snapshot
        :param image_type: snapshot | backup
        :param extra_properties: dict of extra image properties to include
        """
        if extra_properties is None:
            extra_properties = {}
        instance_uuid = instance['uuid']
        properties = {
            'instance_uuid': instance_uuid,
            'user_id': str(context.user_id),
            'image_type': image_type,
        }
        image_ref = instance.image_ref
        image_api_ref = self.image_api

        image_system_meta = {}
        if image_ref is not None and image_ref != '':
            try:
                image = image_api_ref.get(context, image_ref)
            except (exception.ImageNotAuthorized,
                    exception.ImageNotFound,
                    exception.Invalid) as e:
                LOG.warning(_LW("Can't access image %(image_id)s: %(error)s"),
                            {"image_id": image_ref, "error": e},
                            instance=instance)
            else:
                flavor = instance.get_flavor()
                image_system_meta = utils.get_system_metadata_from_image(image,
                                                                         flavor)
        system_meta = utils.instance_sys_meta(instance)
        system_meta.update(image_system_meta)

        sent_meta = utils.get_image_from_system_metadata(system_meta)
        sent_meta['name'] = name
        sent_meta['is_public'] = False
        # The properties set up above and in extra_properties have precedence
        properties.update(extra_properties or {})
        sent_meta['properties'].update(properties)
        return image_api_ref.create(context, sent_meta)

    @nova_api.check_instance_state(vm_state=[vm_states.ACTIVE])
    def cloudlet_create_base(self, context, instance, base_name,
                             extra_properties=None):
        # add network info
        vifs = self.nova_api.network_api.get_vifs_by_instance(context,
                                                              instance)
        net_info = []
        for vif in vifs:
            vif_info = {'id': vif['uuid'], 'mac_address': vif['address']}
            net_info.append(vif_info)

        # add instance resource info
        base_sha256_uuid = sha256(str(instance['uuid'])).hexdigest()

        disk_properties = {
            CloudletAPI.PROPERTY_KEY_CLOUDLET: True,
            CloudletAPI.PROPERTY_KEY_CLOUDLET_TYPE:
            CloudletAPI.IMAGE_TYPE_BASE_DISK,
            CloudletAPI.PROPERTY_KEY_NETWORK_INFO: net_info,
            CloudletAPI.PROPERTY_KEY_BASE_UUID: base_sha256_uuid, }
        mem_properties = {
            CloudletAPI.PROPERTY_KEY_CLOUDLET: True,
            CloudletAPI.PROPERTY_KEY_CLOUDLET_TYPE:
            CloudletAPI.IMAGE_TYPE_BASE_MEM,
            CloudletAPI.PROPERTY_KEY_NETWORK_INFO: net_info,
            CloudletAPI.PROPERTY_KEY_BASE_UUID: base_sha256_uuid, }
        diskhash_properties = {
            CloudletAPI.PROPERTY_KEY_CLOUDLET: True,
            CloudletAPI.PROPERTY_KEY_CLOUDLET_TYPE:
            CloudletAPI.IMAGE_TYPE_BASE_DISK_HASH,
            CloudletAPI.PROPERTY_KEY_NETWORK_INFO: net_info,
            CloudletAPI.PROPERTY_KEY_BASE_UUID: base_sha256_uuid, }
        memhash_properties = {
            CloudletAPI.PROPERTY_KEY_CLOUDLET: True,
            CloudletAPI.PROPERTY_KEY_CLOUDLET_TYPE:
            CloudletAPI.IMAGE_TYPE_BASE_MEM_HASH,
            CloudletAPI.PROPERTY_KEY_NETWORK_INFO: net_info,
            CloudletAPI.PROPERTY_KEY_BASE_UUID: base_sha256_uuid, }
        disk_properties.update(extra_properties or {})
        mem_properties.update(extra_properties or {})
        diskhash_properties.update(extra_properties or {})
        memhash_properties.update(extra_properties or {})

        disk_name = base_name+'-disk'
        diskhash_name = base_name+'-disk-meta'
        mem_name = base_name+'-mem'
        memhash_name = base_name+'-mem-meta'
        snapshot = 'snapshot'

        recv_mem_meta = self._cloudlet_create_image(
            context, instance, mem_name, snapshot,
            extra_properties=mem_properties)
        recv_diskhash_meta = self._cloudlet_create_image(
            context, instance, diskhash_name, snapshot,
            extra_properties=diskhash_properties)
        recv_memhash_meta = self._cloudlet_create_image(
            context, instance, memhash_name, snapshot,
            extra_properties=memhash_properties)

        # add reference for the other base vm information to get it later
        disk_properties.update({
            CloudletAPI.IMAGE_TYPE_BASE_MEM: recv_mem_meta['id'],
            CloudletAPI.IMAGE_TYPE_BASE_DISK_HASH: recv_diskhash_meta['id'],
            CloudletAPI.IMAGE_TYPE_BASE_MEM_HASH: recv_memhash_meta['id'],
            })
        recv_disk_meta = self._cloudlet_create_image(
            context, instance, disk_name, snapshot,
            extra_properties=disk_properties)

        instance.task_state = task_states.IMAGE_SNAPSHOT
        instance.save(expected_task_state=[None])

        # api request
        client = self.router.clients['default'].client
        version = self.router.target.version
        cctxt = client.prepare(
            server=nova_rpc._compute_host(None, instance), version=version
        )
        cctxt.call(context, 'cloudlet_create_base',
                   instance=instance,
                   vm_name=base_name,
                   disk_meta_id=recv_disk_meta['id'],
                   memory_meta_id=recv_mem_meta['id'],
                   diskhash_meta_id=recv_diskhash_meta['id'],
                   memoryhash_meta_id=recv_memhash_meta['id']
                   )
        return recv_disk_meta, recv_mem_meta

    def _create_reservations(self, context, instance, original_task_state, project_id, user_id):
        instance_vcpus = instance.vcpus
        instance_memory_mb = instance.memory_mb
        # NOTE(wangpan): if the instance is resizing, and the resources
        #                are updated to new instance type, we should use
        #                the old instance type to create reservation.
        # see https://bugs.launchpad.net/nova/+bug/1099729 for more details

        quotas = objects.Quotas(context)
        quotas.reserve(project_id=project_id,
                       user_id=user_id,
                       instances=-1,
                       cores=-instance_vcpus,
                       ram=-instance_memory_mb
                       )
        return quotas

    @nova_api.check_instance_state(vm_state=[vm_states.ACTIVE])
    def cloudlet_create_overlay_finish(self, context, instance,
                                       overlay_name, extra_properties=None):
        project_id, user_id = quotas_obj.ids_from_instance(context, instance)
        original_task_state = instance.task_state
        quotas = self._create_reservations(context, instance, original_task_state, project_id, user_id)
        overlay_meta_properties = {
            CloudletAPI.PROPERTY_KEY_CLOUDLET: True,
            CloudletAPI.PROPERTY_KEY_CLOUDLET_TYPE:
            CloudletAPI.IMAGE_TYPE_OVERLAY, }
        overlay_meta_properties.update(extra_properties or {})
        recv_overlay_meta = self._cloudlet_create_image(
            context, instance, overlay_name, 'snapshot',
            extra_properties=overlay_meta_properties)

        instance.task_state = task_states.IMAGE_SNAPSHOT
        instance.save(expected_task_state=[None])

        # api request
        client = self.router.clients['default'].client
        version = self.router.target.version
        cctxt = client.prepare(
            server=nova_rpc._compute_host(None, instance), version=version
        )
        cctxt.cast(context, 'cloudlet_overlay_finish',
                   instance=instance,
                   reservations=quotas.reservations,
                   overlay_name=overlay_name,
                   overlay_id=recv_overlay_meta['id'])
        return recv_overlay_meta

    @nova_api.check_instance_state(vm_state=[vm_states.ACTIVE])
    def cloudlet_handoff(self, context, instance, handoff_url,
                         glance_url, neutron_url, dest_token=None,
                         dest_vmname=None, dest_network=None, extra_properties=None):
        project_id, user_id = quotas_obj.ids_from_instance(context, instance)
        original_task_state = instance.task_state
        quotas = self._create_reservations(context, instance, original_task_state, project_id, user_id)
        recv_residue_meta = None
        parsed_handoff_url = urlsplit(handoff_url)
        residue_glance_id = None
        if parsed_handoff_url.scheme == "file":
            # save the VM residue to glance file
            dest_vm_name = parsed_handoff_url.netloc
            residue_meta_properties = {
                CloudletAPI.PROPERTY_KEY_CLOUDLET: True,
                CloudletAPI.PROPERTY_KEY_CLOUDLET_TYPE:
                CloudletAPI.IMAGE_TYPE_OVERLAY, }
            residue_meta_properties.update(extra_properties or {})
            recv_residue_meta = self._cloudlet_create_image(
                context, instance, dest_vm_name, 'snapshot',
                extra_properties=residue_meta_properties
            )
            instance.task_state = task_states.IMAGE_SNAPSHOT
            instance.save(expected_task_state=[None])
            residue_glance_id = recv_residue_meta['id']
        elif parsed_handoff_url.scheme == "http":
            # handoff to other OpenStack
            # Send message to the destination
            ret_value = self._prepare_handoff_dest(urlparse(handoff_url),
                                                   urlparse(glance_url),
                                                   urlparse(neutron_url),
                                                   dest_token,
                                                   instance,
                                                   dest_vmname,
                                                   dest_network)
            # parse handoff URL from the return
            handoff_dest_addr = ret_value.get("handoff", None)
            if handoff_dest_addr is None:
                msg = "Cannot get handoff URL from the destination message"
                raise HandoffError(msg)
            handoff_url = "tcp://%s:%s" % (handoff_dest_addr['server_ip'],
                                           handoff_dest_addr['server_port'])

        # api request
        client = self.router.clients['default'].client
        version = self.router.target.version
        cctxt = client.prepare(
            server=nova_rpc._compute_host(None, instance), version=version
        )
        cctxt.cast(context, 'cloudlet_handoff',
                   instance=instance,reservations=quotas.reservations,
                   handoff_url=handoff_url,
                   residue_glance_id=residue_glance_id)
        return residue_glance_id

    def _prepare_handoff_dest(self, end_point, glance_url,
                              neutron_url, dest_token, instance,
                              dest_vmname=None, dest_network=None):
        # information of current VM at source
        if dest_vmname:
            instance_name = dest_vmname
        else:
            instance_name = instance['display_name'] + "-handoff"
        flavor_memory = instance['memory_mb']
        flavor_cpu = instance['vcpus']
        requested_basevm_id = instance['system_metadata']['image_base_sha256_uuid']
        original_overlay_url = \
            instance.get("metadata", dict()).get("overlay_url", None)

        # testing image api v2
        image_list = self._get_server_info(glance_url, dest_token, "images", "v2")
        basevm_uuid = None
        for image_item in image_list:
            properties = image_item.get("cloudlet_type", None)
            if properties is None or len(properties) == 0:
                continue
            if properties != CloudletAPI.IMAGE_TYPE_BASE_DISK:
                continue
            base_sha256_uuid = image_item.get(CloudletAPI.PROPERTY_KEY_BASE_UUID)
            if base_sha256_uuid == requested_basevm_id:
                basevm_uuid = image_item['id']
                break
        if basevm_uuid is None:
            msg = "Cannot find matching Base VM with (%s) at (%s)" % \
                  (str(requested_basevm_id), end_point.netloc)
            raise HandoffError(msg)

        # testing networking api v2.0
        newtork_list = self._get_server_info(neutron_url, dest_token, "networks", "v2.0")
        for network_item in newtork_list:
            if network_item['project_id'] != instance.project_id:
                continue
            network_uuid = None
            if network_item['name'] == dest_network:
                network_uuid = network_item['id']
        if network_uuid is None:
            msg = "Cannot find Networking with (%s) at (%s)" % \
                  (str(network_uuid), end_point.netloc)
            raise HandoffError(msg)

        # Find matching flavor.
        def find_matching_flavor(flavor_list, cpu_count, memory_mb):
            for flavor in flavor_list:
                vcpu = int(flavor['vcpus'])
                ram_mb = int(flavor['ram'])
                if vcpu == cpu_count and ram_mb == memory_mb:
                    flavor_ref = flavor['links'][0]['href']
                    flavor_id = flavor['id']
                    return flavor_ref, flavor_id
            return None, None
        flavor_list = self._get_server_info(end_point, dest_token, "flavors")
        flavor_ref, flavor_id = find_matching_flavor(
            flavor_list, flavor_cpu, flavor_memory)
        if flavor_ref is None or flavor_id is None:
            msg = "Cannot find matching flavor with cpu=%d, memory=%d at %s" %\
                (flavor_cpu, flavor_memory, end_point.netloc)
            raise HandoffError(msg)

        # generate request
        meta_data = {
            "handoff_info": instance_name,
            "overlay_url": original_overlay_url
        }

        s = {
            "server": {
                "name": instance_name, "imageRef": str(basevm_uuid),
                "flavorRef": flavor_id, "metadata": meta_data,
                "min_count": "1", "max_count": "1",
                "networks": [{
                    "uuid": network_uuid
                }],
                # "key_name": None,
            }
        }
        params = jsonutils.dumps(s)
        headers = {
            "X-Auth-Token": dest_token,
            "Content-type": "application/json"}
        conn = httplib.HTTPConnection(end_point[1])
        conn.request("POST", "%s/servers" % end_point[2], params, headers)
        LOG.info("request handoff to %s" % (end_point.netloc))
        response = conn.getresponse()
        data = response.read()
        dd = jsonutils.loads(data)
        conn.close()

        return dd

    def _get_server_info(self, end_point, token, request_list, version=None):
        if not request_list in ('images', 'flavors', 'extensions', 'servers', 'networks'):
            LOG.debug("Error, Cannot support listing for %s\n" % request_list)
            return None

        params = ''
        headers = {"X-Auth-Token": token, "Content-type": "application/json"}
        if request_list == 'extensions':
            end_string = "%s/%s" % (end_point[2], request_list)
        elif request_list == 'images':
            if version is None:
                end_string = "%s/%s/detail" % (end_point[2], request_list)
            else:
                end_string = "/%s/%s" % (version, request_list)
        elif request_list == 'networks':
            end_string = "/%s/%s" % (version, request_list)
        else:
            end_string = "%s/%s/detail" % (end_point[2], request_list)

        # HTTP response
        conn = httplib.HTTPConnection(end_point[1])
        conn.request("GET", end_string, params, headers)
        response = conn.getresponse()
        data = response.read()
        dd = jsonutils.loads(data)
        conn.close()
        return dd[request_list]

    def handoff_port_forwarding(self, dest_ip, dest_port):
        # type(dest_ip) = netaddr.ip.IPAddress at kilo
        o = PortForwarding(str(dest_ip), int(dest_port))
        # port forwarding server will finish automatically whne a client
        # disconnects
        o.start()
        return o.source_port


class PortForwarding(threading.Thread):
    """Forward VM handoff packet to the compute node."""

    def __init__(self, dest_ip, dest_port, source_port=None):
        self.dest_ip = dest_ip
        self.dest_port = dest_port
        if source_port is None:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(('0.0.0.0', 0))   # get free TCP port
            local_port = int(sock.getsockname()[1])
            self.source_port = local_port
        else:
            self.source_port = source_port
        threading.Thread.__init__(self, target=self.port_forwarding)

    def port_forwarding(self):
        local_addr = ('0.0.0.0', self.source_port)
        remote_addr = (self.dest_ip, self.dest_port)
        LOG.info("Port forwarding starts from %s to %s" % (str(local_addr),
                                                           str(remote_addr)))
        listener = eventlet.listen(local_addr)
        client, addr = listener.accept()
        server = eventlet.connect(remote_addr)
        eventlet.spawn_n(self.forward, client, server, self.closed_callback)
        eventlet.spawn_n(self.forward, server, client)

    def closed_callback(self):
        LOG.info("Port forwadring finishes")

    def forward(self, source, dest, cb=lambda: None):
        while True:
            d = source.recv(32384)
            if d == '':
                cb()
                break
            dest.sendall(d)
