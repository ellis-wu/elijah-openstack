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


import os
import uuid
import hashlib
import subprocess
import select
import shutil
import StringIO
import errno
import functools
from urlparse import urlsplit
from tempfile import mkdtemp    # replace it to util.tempdir
from oslo_log import log as logging
from nova.i18n import _
from oslo_utils import fileutils
from oslo_service import loopingcall

from nova.virt.libvirt import blockinfo
from nova.compute import power_state
from nova import exception
from nova import exception_wrapper
from nova import utils
from nova.virt import driver
from nova.virt.libvirt import utils as libvirt_utils
from nova.virt.libvirt import driver as libvirt_driver
from nova.virt.libvirt import guest as libvirt_guest
from nova.image import glance
from nova.compute import task_states
from nova.compute.cloudlet_api import CloudletAPI

from xml.etree import ElementTree
from elijah.provisioning import synthesis
from elijah.provisioning import handoff
try:
    from elijah.provisioning import msgpack
except ImportError as e:
    import msgpack
from elijah.provisioning import compression
from elijah.provisioning.package import VMOverlayPackage
from elijah.provisioning.configuration import Const as Cloudlet_Const
from elijah.provisioning.configuration import Options


LOG = logging.getLogger(__name__)
synthesis.LOG = LOG  # overwrite cloudlet's own log


class CloudletDriver(libvirt_driver.LibvirtDriver):

    def __init__(self, read_only=False):
        super(CloudletDriver, self).__init__(read_only)

        # manage VM overlay list
        self.resumed_vm_dict = dict()
        # manage synthesized VM list
        self.synthesized_vm_dics = dict()

    def _get_snapshot_metadata(self, virt_dom, context, instance, snapshot_id):
        _image_service = glance.get_remote_image_service(context, snapshot_id)
        snapshot_image_service, snapshot_image_id = _image_service
        snapshot = snapshot_image_service.show(context, snapshot_image_id)
        snapshot_props = snapshot.get('properties')

        metadata = {
            'is_public': True,
            'status': 'active',
            'name': snapshot['name'],
            'properties': {
                'kernel_id': instance['kernel_id'],
                'image_location': 'snapshot',
                'image_state': 'available',
                'owner_id': instance['project_id'],
                'ramdisk_id': instance['ramdisk_id'],
                'user_id': snapshot_props['user_id'],
                'base_image_ref': snapshot_props['base_image_ref'],
                'base_resource_xml_str': snapshot_props['base_resource_xml_str'],
                'base_sha256_uuid': snapshot_props['base_sha256_uuid'],
                'cloudlet_base_disk_hash': snapshot_props['cloudlet_base_disk_hash'],
                'cloudlet_base_memory': snapshot_props['cloudlet_base_memory'],
                'cloudlet_base_memory_hash': snapshot_props['cloudlet_base_memory_hash'],
                'cloudlet_type': snapshot_props['cloudlet_type'],
                'instance_uuid': snapshot_props['instance_uuid'],
                'is_cloudlet': snapshot_props['is_cloudlet'],
            }
        }

        (image_service, image_id) = glance.get_remote_image_service(
            context, instance['image_ref'])
        try:
            base = image_service.show(context, image_id)
        except exception.ImageNotFound:
            base = {}

        if 'architecture' in base.get('properties', {}):
            arch = base['properties']['architecture']
            metadata['properties']['architecture'] = arch

        metadata['disk_format'] = 'raw'
        metadata['container_format'] = base.get('container_format', 'bare')
        return metadata

    def _update_to_glance(self, context, image_service, filepath,
                          meta_id, metadata):
        with libvirt_utils.file_open(filepath) as image_file:
            image_service.update(context,
                                 meta_id,
                                 metadata,
                                 image_file)

    @exception_wrapper.wrap_exception()
    def cloudlet_base(self, context, instance, vm_name,
                      disk_meta_id, memory_meta_id,
                      diskhash_meta_id, memoryhash_meta_id, update_task_state):
        """create base vm and save it to glance
        """
        try:
            virt_dom = self._host.get_domain(instance)
        except exception.InstanceNotFound:
            raise exception.InstanceNotRunning(instance_id=instance['uuid'])

        # pause VM
        self.pause(instance)

        (image_service, image_id) = glance.get_remote_image_service(
            context, instance['image_ref'])

        disk_metadata = self._get_snapshot_metadata(virt_dom, context,
                                                    instance, disk_meta_id)
        mem_metadata = self._get_snapshot_metadata(virt_dom, context,
                                                   instance, memory_meta_id)
        diskhash_metadata = self._get_snapshot_metadata(
            virt_dom, context, instance, diskhash_meta_id)
        memhash_metadata = self._get_snapshot_metadata(
            virt_dom, context, instance, memoryhash_meta_id)

        disk_path = libvirt_utils.find_disk(virt_dom)
        source_format = libvirt_utils.get_disk_type_from_path(disk_path)
        snapshot_name = uuid.uuid4().hex
        (state, _max_mem, _mem, _cpus, _t) = virt_dom.info()
        state = libvirt_guest.LIBVIRT_POWER_STATE[state]

        # creating base vm requires cold snapshotting
        snapshot_backend = self.image_backend.snapshot(
            disk_path,
            image_type=source_format)

        LOG.info(_("Beginning cold snapshot process"),
                 instance=instance)
        # not available at icehouse
        # snapshot_backend.snapshot_create()

        update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD,
                          expected_state=None)
        snapshot_directory = libvirt_driver.CONF.libvirt.snapshots_directory
        fileutils.ensure_tree(snapshot_directory)
        with utils.tempdir(dir=snapshot_directory) as tmpdir:
            try:
                out_path = os.path.join(tmpdir, snapshot_name)
                # At this point, base vm should be "raw" format
                snapshot_backend.snapshot_extract(out_path, "raw")
            finally:
                # snapshotting logic is changed since icehouse.
                #  : cannot find snapshot_create and snapshot_delete.
                # snapshot_extract is replacing these two operations.
                # snapshot_backend.snapshot_delete()
                LOG.info(_("Snapshot extracted, beginning image upload"),
                         instance=instance)

            # generate memory snapshop and hashlist
            basemem_path = os.path.join(tmpdir, snapshot_name+"-mem")
            diskhash_path = os.path.join(tmpdir, snapshot_name+"-disk_hash")
            memhash_path = os.path.join(tmpdir, snapshot_name+"-mem_hash")

            update_task_state(task_state=task_states.IMAGE_UPLOADING,
                              expected_state=task_states.IMAGE_PENDING_UPLOAD)
            synthesis._create_baseVM(self._conn,
                                     virt_dom,
                                     out_path,
                                     basemem_path,
                                     diskhash_path,
                                     memhash_path,
                                     nova_util=libvirt_utils)

            self._update_to_glance(context, image_service, out_path,
                                   disk_meta_id, disk_metadata)
            LOG.info(_("Base disk upload complete"), instance=instance)
            self._update_to_glance(context, image_service, basemem_path,
                                   memory_meta_id, mem_metadata)
            LOG.info(_("Base memory image upload complete"), instance=instance)
            self._update_to_glance(context, image_service, diskhash_path,
                                   diskhash_meta_id, diskhash_metadata)
            LOG.info(_("Base disk upload complete"), instance=instance)
            self._update_to_glance(context, image_service, memhash_path,
                                   memoryhash_meta_id, memhash_metadata)
            LOG.info(_("Base memory image upload complete"), instance=instance)

    def _create_network_only(self, xml, instance, network_info,
                             block_device_info=None):
        """Only perform network setup but skip set-up for domain (vm instance)
        because cloudlet code takes care of domain
        """
        block_device_mapping = driver.block_device_info_get_mapping(
            block_device_info)

        for vol in block_device_mapping:
            connection_info = vol['connection_info']
            disk_dev = vol['mount_device'].rpartition("/")[2]
            disk_info = {
                'dev': disk_dev,
                'bus': blockinfo.get_disk_bus_for_disk_dev(
                    libvirt_driver.CONF.libvirt.virt_type, disk_dev
                    ),
                'type': 'disk',
                }
            self._connect_volume(connection_info,
                                 disk_info
                                 )

        self.plug_vifs(instance, network_info)
        self.firewall_driver.setup_basic_filtering(instance, network_info)
        self.firewall_driver.prepare_instance_filter(instance, network_info)
        self.firewall_driver.apply_instance_filter(instance, network_info)

    def create_overlay_vm(self, context, instance,
                          overlay_name, overlay_id, update_task_state):
        try:
            virt_dom = self._host.get_domain(instance)
        except exception.InstanceNotFound:
            raise exception.InstanceNotRunning(instance_id=instance['uuid'])

        # make sure base vm is cached
        (image_service, image_id) = glance.get_remote_image_service(
            context, instance['image_ref'])
        image_meta = image_service.show(context, image_id)
        base_sha256_uuid, memory_snap_id, diskhash_snap_id, memhash_snap_id = \
            self._get_basevm_meta_info(image_meta)
        self._get_cache_image(context, instance, image_meta['id'])
        self._get_cache_image(context, instance, memory_snap_id)
        self._get_cache_image(context, instance, diskhash_snap_id)
        self._get_cache_image(context, instance, memhash_snap_id)

        # remove neutron network interface
        vir_xml = ElementTree.fromstring(virt_dom.XMLDesc())
        nic_xml = vir_xml.findall('devices/interface')
        neutron_nic = None
        for nic in nic_xml:
            if nic.get('type') == 'bridge':
                neutron_nic = ElementTree.tostring(nic)

        if neutron_nic is not None:
            if virt_dom.detachDevice(neutron_nic) != 0:
                LOG.info("neutron network detach failed")

        # pause VM
        self.pause(instance)

        # create VM overlay
        (image_service, image_id) = glance.get_remote_image_service(
            context, instance['image_ref'])
        meta_metadata = self._get_snapshot_metadata(virt_dom, context,
                                                    instance, overlay_id)
        update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD,
                          expected_state=None)

        vm_overlay = self.resumed_vm_dict.get(instance['uuid'], None)
        if vm_overlay is None:
            raise exception.InstanceNotRunning(instance_id=instance['uuid'])
        del self.resumed_vm_dict[instance['uuid']]
        vm_overlay.create_overlay()
        overlay_zip = vm_overlay.overlay_zipfile
        LOG.info("overlay : %s" % str(overlay_zip))

        update_task_state(task_state=task_states.IMAGE_UPLOADING,
                          expected_state=task_states.IMAGE_PENDING_UPLOAD)

        # export to glance
        self._update_to_glance(context, image_service, overlay_zip,
                               overlay_id, meta_metadata)
        LOG.info(_("overlay_vm upload complete"), instance=instance)

        if os.path.exists(overlay_zip):
            os.remove(overlay_zip)

    def perform_vmhandoff(self, context, instance, handoff_url,
                          update_task_state, residue_glance_id=None):
        try:
            virt_dom = self._host.get_domain(instance)
        except exception.InstanceNotFound:
            raise exception.InstanceNotRunning(instance_id=instance['uuid'])
        synthesized_vm = self.synthesized_vm_dics.get(instance['uuid'], None)
        if synthesized_vm is None:
            raise exception.InstanceNotRunning(instance_id=instance['uuid'])

        # get the file path for Base VM and VM overlay
        (image_service, image_id) = glance.get_remote_image_service(
            context, instance['image_ref'])
        image_meta = image_service.show(context, image_id)
        base_sha256_uuid, memory_snap_id, diskhash_snap_id, memhash_snap_id = \
            self._get_basevm_meta_info(image_meta)
        basedisk_path = self._get_cache_image(
            context, instance, image_meta['id'])
        basemem_path = self._get_cache_image(context, instance, memory_snap_id)
        diskhash_path = self._get_cache_image(
            context, instance, diskhash_snap_id)
        memhash_path = self._get_cache_image(
            context, instance, memhash_snap_id)
        base_vm_paths = [basedisk_path, basemem_path,
                         diskhash_path, memhash_path]

        # remove neutron network interface
        vir_xml = ElementTree.fromstring(virt_dom.XMLDesc())
        nic_xml = vir_xml.findall('devices/interface')
        neutron_nic = None
        for nic in nic_xml:
            if nic.get('type') == 'bridge':
                neutron_nic = ElementTree.tostring(nic)

        if neutron_nic is not None:
            if virt_dom.detachDevice(neutron_nic) != 0:
                LOG.info("neutron network detach failed")

        update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD,
                          expected_state=None)
        try:
            residue_filepath = self._handoff_send(
                base_vm_paths, base_sha256_uuid, synthesized_vm, handoff_url
            )
        except handoff.HandoffError as e:
            msg = "failed to perform VM handoff:\n"
            msg += str(e)
            raise exception.ImageNotFound(msg)

        del self.synthesized_vm_dics[instance['uuid']]
        if residue_filepath:
            LOG.info("residue saved at %s" % residue_filepath)
        if residue_filepath and residue_glance_id:
            # export to glance
            (image_service, image_id) = glance.get_remote_image_service(
                context, instance['image_ref'])
            meta_metadata = self._get_snapshot_metadata(
                virt_dom,
                context,
                instance,
                residue_glance_id)
            update_task_state(task_state=task_states.IMAGE_UPLOADING,
                              expected_state=task_states.IMAGE_PENDING_UPLOAD)
            self._update_to_glance(context, image_service, residue_filepath,
                                   residue_glance_id, meta_metadata)
        # clean up
        LOG.info(_("VM residue upload complete"), instance=instance)
        if residue_filepath and os.path.exists(residue_filepath):
            os.remove(residue_filepath)

    def _handoff_send(self, base_vm_paths, base_hashvalue,
                      synthesized_vm, handoff_url):
        """
        """
        # preload basevm hash dictionary for creating residue
        (basedisk_path, basemem_path,
         diskhash_path, memhash_path) = base_vm_paths
        preload_thread = handoff.PreloadResidueData(diskhash_path, memhash_path)
        preload_thread.start()
        preload_thread.join()

        options = Options()
        options.TRIM_SUPPORT = True
        options.FREE_SUPPORT = True
        options.DISK_ONLY = False

        # Set up temp file path for data structure and residue
        residue_tmp_dir = mkdtemp(prefix="cloudlet-residue-")
        handoff_send_datafile = os.path.join(residue_tmp_dir, "handoff_data")

        residue_zipfile = None
        dest_handoff_url = handoff_url
        parsed_handoff_url = urlsplit(handoff_url)
        if parsed_handoff_url.scheme == "file":
            residue_zipfile = os.path.join(
                residue_tmp_dir, Cloudlet_Const.OVERLAY_ZIP)
            dest_handoff_url = "file://%s" % os.path.abspath(residue_zipfile)

        # handoff mode --> fix it to be serializable
        handoff_mode = None  # use default

        # data structure for handoff sending
        handoff_ds_send = handoff.HandoffDataSend()
        LOG.debug("save handoff data to %s" % handoff_send_datafile)

        libvirt_uri = self._uri()
        handoff_ds_send.save_data(
            base_vm_paths, base_hashvalue,
            preload_thread.basedisk_hashdict,
            preload_thread.basemem_hashdict,
            options, dest_handoff_url, handoff_mode,
            synthesized_vm.fuse.mountpoint, synthesized_vm.qemu_logfile,
            synthesized_vm.qmp_channel, synthesized_vm.machine.ID(),
            synthesized_vm.fuse.modified_disk_chunks, libvirt_uri,
        )

        LOG.debug("start handoff send process")
        handoff_ds_send.to_file(handoff_send_datafile)
        cmd = ["/usr/local/bin/handoff-proc", "%s" % handoff_send_datafile]
        LOG.debug("subprocess: %s" % cmd)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, close_fds=True)

        def _wait_for_handoff_send(print_log=False):
            """Called at an interval until VM synthesis finishes."""
            returncode = proc.poll()
            if returncode is None:
                # keep record stdout
                LOG.debug("waiting for finishing handoff send")
                in_ready, _, _ = select.select([proc.stdout], [], [])
                try:
                    buf = os.read(proc.stdout.fileno(), 1024*100)
                    if print_log:
                        LOG.debug(buf)
                except OSError as e:
                    if e.errno == errno.EAGAIN or e.errno == errno.EWOULDBLOCK:
                        return
            else:
                raise loopingcall.LoopingCallDone()
        timer = loopingcall.FixedIntervalLoopingCall(
            _wait_for_handoff_send,
            print_log=True)
        timer.start(interval=0.5).wait()
        LOG.info("Handoff send finishes")
        return residue_zipfile

    def _get_cache_image(self, context, instance, snapshot_id, suffix=''):
        def basepath(fname='', suffix=suffix):
            return os.path.join(libvirt_utils.get_instance_path(instance),
                                fname + suffix)

        def raw(fname, image_type='raw'):
            return self.image_backend.image(instance, fname, image_type)

        # ensure directories exist and are writable
        fileutils.ensure_tree(basepath(suffix=''))
        fname = hashlib.sha1(snapshot_id).hexdigest()
        LOG.debug(_("cloudlet, caching file at %s" % fname))
        size = instance['root_gb'] * 1024 * 1024 * 1024
        if size == 0:
            size = None

        raw('disk').cache(fetch_func=libvirt_utils.fetch_image,
                          filename=fname,
                          size=size,
                          context=context,
                          image_id=snapshot_id)

        # from cache method at virt/libvirt/imagebackend.py
        abspath = os.path.join(
            libvirt_driver.CONF.instances_path,
            libvirt_driver.CONF.image_cache_subdirectory_name,
            fname)
        return abspath

    def _polish_VM_configuration(self, xml):
        # remove cpu element
        cpu_element = xml.find("cpu")
        if cpu_element is not None:
            xml.remove(cpu_element)

        # TODO: Handle console/serial element properly
        device_element = xml.find("devices")
        console_elements = device_element.findall("console")
        for console_element in console_elements:
            device_element.remove(console_element)
        serial_elements = device_element.findall("serial")
        for serial_element in serial_elements:
            device_element.remove(serial_element)

        # remove O_DIRECT option since FUSE does not support it
        disk_elements = xml.findall('devices/disk')
        for disk_element in disk_elements:
            disk_type = disk_element.attrib['device']
            if disk_type == 'disk':
                hdd_driver = disk_element.find("driver")
                if hdd_driver is not None and hdd_driver.get("cache", None) is not None:
                    del hdd_driver.attrib['cache']

        xml_str = ElementTree.tostring(xml)
        return xml_str

    def _get_basevm_meta_info(self, image_meta):
        # get memory_snapshot_id for resume case
        base_sha256_uuid = None
        memory_snap_id = None
        diskhash_snap_id = None
        memhash_snap_id = None

        meta_data = image_meta.get('properties', None)
        if meta_data and meta_data.get(CloudletAPI.IMAGE_TYPE_BASE_MEM):
            base_sha256_uuid = str(
                meta_data.get(CloudletAPI.PROPERTY_KEY_BASE_UUID))
            memory_snap_id = str(
                meta_data.get(CloudletAPI.IMAGE_TYPE_BASE_MEM))
            diskhash_snap_id = str(
                meta_data.get(CloudletAPI.IMAGE_TYPE_BASE_DISK_HASH))
            memhash_snap_id = str(
                meta_data.get(CloudletAPI.IMAGE_TYPE_BASE_MEM_HASH))
            LOG.debug(
                _("cloudlet, get base sha256 uuid: %s" % str(base_sha256_uuid)))
            LOG.debug(
                _("cloudlet, get memory_snapshot_id: %s" % str(memory_snap_id)))
            LOG.debug(
                _("cloudlet, get disk_hash_id: %s" % str(diskhash_snap_id)))
            LOG.debug(
                _("cloudlet, get memory_hash_id: %s" % str(memhash_snap_id)))
        return base_sha256_uuid, memory_snap_id, diskhash_snap_id, memhash_snap_id

    def _get_VM_overlay_url(self, instance):
        # get overlay from instance metadata for synthesis case
        overlay_url = None
        instance_meta = instance.get('metadata', None)
        if instance_meta is not None:
            for (key, value) in instance_meta.iteritems():
                if key == "overlay_url":
                    overlay_url = value
        return overlay_url

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None):
        """overwrite original libvirt_driver's spawn method
        """
        # add metadata to the instance
        def _append_metadata(target_instance, metadata_dict):
            original_meta = target_instance.get('metadata', None) or list()
            original_meta.append(metadata_dict)
            target_instance['metadata'] = original_meta

        # get meta info related to VM synthesis
        (image_service, image_id) = glance.get_remote_image_service(
            context, instance['image_ref'])
        image_detail_meta = image_service.show(context, image_id)

        base_sha256_uuid, memory_snap_id, diskhash_snap_id, memhash_snap_id = \
            self._get_basevm_meta_info(image_detail_meta)

        overlay_url = None
        handoff_info = None
        instance_meta = instance.get('metadata', None)
        if instance_meta is not None:
            if "overlay_url" in instance_meta.keys():
                overlay_url = instance_meta.get("overlay_url")
            if "handoff_info" in instance_meta.keys():
                handoff_info = instance_meta.get("handoff_info")

        # original openstack logic
        disk_info = blockinfo.get_disk_info(libvirt_driver.CONF.libvirt.virt_type,
                                            instance,
                                            image_meta,
                                            block_device_info)

        gen_confdrive = functools.partial(self._create_configdrive,
                                          context, instance,
                                          admin_pass=admin_password,
                                          files=injected_files,
                                          network_info=network_info)

        self._create_image(context, instance,
                           disk_info['mapping'],
                           network_info=network_info,
                           block_device_info=block_device_info,
                           files=injected_files,
                           admin_pass=admin_password)

        # Required by Quobyte CI
        self._ensure_console_log_for_instance(instance)

        xml = self._get_guest_xml(context, instance, network_info,
                                  disk_info, image_meta,
                                  block_device_info=block_device_info
                                  )

        # handle xml configuration to make a portable VM
        xml_obj = ElementTree.fromstring(xml)
        xml = self._polish_VM_configuration(xml_obj)

        # avoid injecting key, password, and metadata since we're resuming VM
        original_inject_password = libvirt_driver.CONF.libvirt.inject_password
        original_inject_key = libvirt_driver.CONF.libvirt.inject_key
        original_metadata = instance.get('metadata')
        libvirt_driver.CONF.libvirt.inject_password = None
        libvirt_driver.CONF.libvirt.inject_key = None
        instance['metadata'] = {}

        # revert back the configuration
        libvirt_driver.CONF.libvirt.inject_password = original_inject_password
        libvirt_driver.CONF.libvirt.inject_key = original_inject_key
        instance['metadata'] = original_metadata

        if (overlay_url is not None) and (handoff_info is None):
            # spawn instance using VM synthesis
            LOG.debug(_('cloudlet, synthesis start'))
            # append metadata to the instance
            self._create_network_only(xml, instance, network_info,
                                      block_device_info)
            synthesized_vm = self._spawn_using_synthesis(context, instance,
                                                         xml, image_meta,
                                                         overlay_url)
            instance_uuid = str(instance.get('uuid', ''))
            self.synthesized_vm_dics[instance_uuid] = synthesized_vm
        elif handoff_info is not None:
            # spawn instance using VM handoff
            LOG.debug(_('cloudlet, Handoff start'))
            self._create_network_only(xml, instance, network_info,
                                      block_device_info)
            synthesized_vm = self._spawn_using_handoff(context, instance,
                                                       xml, image_meta,
                                                       handoff_info)
            instance_uuid = str(instance.get('uuid', ''))
            self.synthesized_vm_dics[instance_uuid] = synthesized_vm
            pass
        elif memory_snap_id is not None:
            # resume from memory snapshot
            LOG.debug(_('cloudlet, resume from memory snapshot'))
            # append metadata to the instance
            basedisk_path = self._get_cache_image(context, instance,
                                                  image_meta.id)
            basemem_path = self._get_cache_image(context, instance,
                                                 memory_snap_id)
            diskhash_path = self._get_cache_image(context, instance,
                                                  diskhash_snap_id)
            memhash_path = self._get_cache_image(context, instance,
                                                 memhash_snap_id)

            LOG.debug(_('cloudlet, creating network'))
            self._create_network_only(xml, instance, network_info,
                                      block_device_info)
            LOG.debug(_('cloudlet, resuming base vm'))
            self.resume_basevm(instance, xml, basedisk_path, basemem_path,
                               diskhash_path, memhash_path, base_sha256_uuid)
        else:
            self._create_domain_and_network(
                context, xml, instance, network_info, disk_info,
                block_device_info=block_device_info,
                post_xml_callback=gen_confdrive,
                destroy_disks_on_failure=True)

        LOG.debug(_("Instance is running"), instance=instance)

        def _wait_for_boot():
            """Called at an interval until the VM is running."""
            state = self.get_info(instance).state

            if state == power_state.RUNNING:
                raise loopingcall.LoopingCallDone()

        timer = loopingcall.FixedIntervalLoopingCall(_wait_for_boot)
        timer.start(interval=0.5).wait()
        LOG.info(_("Instance spawned successfully."),
                 instance=instance)

    def _destroy(self, instance, attempt=1):
        """overwrite original libvirt_driver's _destroy method
        """
        super(CloudletDriver, self)._destroy(instance)

        # get meta info related to VM synthesis
        instance_uuid = str(instance.get('uuid', ''))

        # check resumed base VM list
        vm_overlay = self.resumed_vm_dict.get(instance_uuid, None)
        if vm_overlay is not None:
            vm_overlay.terminate()
            del self.resumed_vm_dict[instance['uuid']]

        # check synthesized VM list
        synthesized_VM = self.synthesized_vm_dics.get(instance_uuid, None)
        if synthesized_VM is not None:
            LOG.info(_("Deallocate all resources of synthesized VM"),
                     instance=instance)
            if hasattr(synthesized_VM, 'machine'):
                # intentionally avoid terminating VM at synthesis code
                # since OpenStack will do that
                synthesized_VM.machine = None
            synthesized_VM.terminate()
            del self.synthesized_vm_dics[instance_uuid]

    def resume_basevm(self, instance, xml, base_disk, base_memory,
                      base_diskmeta, base_memmeta, base_hashvalue):
        """ resume base vm to create overlay vm
        """
        options = synthesis.Options()
        options.TRIM_SUPPORT = True
        options.FREE_SUPPORT = False
        options.XRAY_SUPPORT = False
        options.DISK_ONLY = False
        options.ZIP_CONTAINER = True
        vm_overlay = synthesis.VM_Overlay(base_disk, options,
                                          base_mem=base_memory,
                                          base_diskmeta=base_diskmeta,
                                          base_memmeta=base_memmeta,
                                          base_hashvalue=base_hashvalue,
                                          nova_xml=xml,
                                          nova_util=libvirt_utils,
                                          nova_conn=self._conn)
        virt_dom = vm_overlay.resume_basevm()
        self.resumed_vm_dict[instance['uuid']] = vm_overlay
        synthesis.rettach_nic(virt_dom, vm_overlay.old_xml_str, xml)

    def _spawn_using_synthesis(self, context, instance, xml,
                               image_meta, overlay_url):
        # download vm overlay
        overlay_package = VMOverlayPackage(overlay_url)
        meta_raw = overlay_package.read_meta()
        meta_info = msgpack.unpackb(meta_raw)
        basevm_sha256 = meta_info.get(Cloudlet_Const.META_BASE_VM_SHA256, None)

        (image_service, image_id) = glance.get_remote_image_service(
            context, instance['image_ref'])
        image_detail_meta = image_service.show(context, image_id)
        image_properties = image_detail_meta.get("properties", None)
        if image_properties is None:
            msg = "image does not have properties for cloudlet metadata"
            raise exception.ImageNotFound(msg)
        image_sha256 = image_properties.get(CloudletAPI.PROPERTY_KEY_BASE_UUID)

        # check basevm
        if basevm_sha256 != image_sha256:
            msg = "requested base vm is not compatible with openstack base disk %s != %s" \
                % (basevm_sha256, image_sha256)
            raise exception.ImageNotFound(msg)
        memory_snap_id = str(
            image_properties.get(CloudletAPI.IMAGE_TYPE_BASE_MEM))
        diskhash_snap_id = str(
            image_properties.get(CloudletAPI.IMAGE_TYPE_BASE_DISK_HASH))
        memhash_snap_id = str(
            image_properties.get(CloudletAPI.IMAGE_TYPE_BASE_MEM_HASH))
        basedisk_path = self._get_cache_image(context, instance,
                                              image_meta.id)
        basemem_path = self._get_cache_image(context, instance, memory_snap_id)
        diskhash_path = self._get_cache_image(context, instance,
                                              diskhash_snap_id)
        memhash_path = self._get_cache_image(context, instance, memhash_snap_id)

        # download blob
        fileutils.ensure_tree(libvirt_utils.get_instance_path(instance))
        decomp_overlay = os.path.join(libvirt_utils.get_instance_path(instance),
            'decomp_overlay')

        meta_info = compression.decomp_overlayzip(overlay_url, decomp_overlay)

        # recover VM
        launch_disk, launch_mem, fuse, delta_proc, fuse_proc = \
            synthesis.recover_launchVM(basedisk_path, meta_info,
                                       decomp_overlay,
                                       base_mem=basemem_path,
                                       base_diskmeta=diskhash_path,
                                       base_memmeta=memhash_path)
        # resume VM
        LOG.info(_("Starting VM synthesis"), instance=instance)
        synthesized_vm = synthesis.SynthesizedVM(launch_disk, launch_mem, fuse,
                                                 disk_only=False,
                                                 qemu_args=False,
                                                 nova_xml=xml,
                                                 nova_conn=self._conn,
                                                 nova_util=libvirt_utils
                                                 )
        # testing non-thread resume
        delta_proc.start()
        fuse_proc.start()
        delta_proc.join()
        fuse_proc.join()
        LOG.info(_("Finish VM synthesis"), instance=instance)
        synthesized_vm.resume()
        # rettach NIC
        synthesis.rettach_nic(synthesized_vm.machine,
                              synthesized_vm.old_xml_str, xml)

        return synthesized_vm

    def _spawn_using_handoff(self, context, instance, xml,
                             image_meta, handoff_info):
        (image_service, image_id) = glance.get_remote_image_service(
            context, instance['image_ref'])
        image_detail_meta = image_service.show(context, image_id)
        image_properties = image_detail_meta.get("properties", None)

        memory_snap_id = str(
            image_properties.get(CloudletAPI.IMAGE_TYPE_BASE_MEM))
        diskhash_snap_id = str(
            image_properties.get(CloudletAPI.IMAGE_TYPE_BASE_DISK_HASH))
        memhash_snap_id = str(
            image_properties.get(CloudletAPI.IMAGE_TYPE_BASE_MEM_HASH))
        basedisk_path = self._get_cache_image(context, instance,
                                              image_meta.id)
        basemem_path = self._get_cache_image(context, instance, memory_snap_id)
        diskhash_path = self._get_cache_image(context, instance,
                                              diskhash_snap_id)
        memhash_path = self._get_cache_image(context, instance,
                                             memhash_snap_id)
        base_vm_paths = [basedisk_path, basemem_path,
                         diskhash_path, memhash_path]
        image_sha256 = image_properties.get(CloudletAPI.PROPERTY_KEY_BASE_UUID)

        snapshot_directory = libvirt_driver.CONF.libvirt.snapshots_directory
        fileutils.ensure_tree(snapshot_directory)
        synthesized_vm = None
        with utils.tempdir(dir=snapshot_directory) as tmpdir:
            uuidhex = uuid.uuid4().hex
            launch_diskpath = os.path.join(tmpdir, uuidhex + "-launch-disk")
            launch_memorypath = os.path.join(
                tmpdir, uuidhex + "-launch-memory")
            tmp_dir = mkdtemp(prefix="cloudlet-residue-")
            handoff_recv_datafile = os.path.join(tmp_dir, "handoff-data")
            # recv handoff data and synthesize disk img and memory snapshot
            try:
                ret_values = self._handoff_recv(base_vm_paths, image_sha256,
                                                handoff_recv_datafile,
                                                launch_diskpath,
                                                launch_memorypath)
                # start VM
                launch_disk_size, launch_memory_size, \
                    disk_overlay_map, memory_overlay_map = ret_values
                synthesized_vm = self._handoff_launch_vm(
                    xml, basedisk_path, basemem_path,
                    launch_diskpath, launch_memorypath,
                    int(launch_disk_size), int(launch_memory_size),
                    disk_overlay_map, memory_overlay_map,
                )

                # rettach NIC
                synthesis.rettach_nic(synthesized_vm.machine,
                                      synthesized_vm.old_xml_str, xml)
            except handoff.HandoffError as e:
                msg = "failed to perform VM handoff:\n"
                msg += str(e)
                raise exception.ImageNotFound(msg)
            finally:
                if os.path.exists(tmp_dir):
                    shutil.rmtree(tmp_dir)
                if os.path.exists(launch_diskpath):
                    os.remove(launch_diskpath)
                if os.path.exists(launch_memorypath):
                    os.remove(launch_memorypath)
        return synthesized_vm

    def _handoff_recv(self, base_vm_paths, base_hashvalue,
                      handoff_recv_datafile, launch_diskpath,
                      launch_memorypath):
        # data structure for handoff receiving
        handoff_ds_recv = handoff.HandoffDataRecv()
        handoff_ds_recv.save_data(
            base_vm_paths, base_hashvalue,
            launch_diskpath, launch_memorypath
        )
        handoff_ds_recv.to_file(handoff_recv_datafile)

        LOG.debug("start handoff recv process")
        cmd = ["/usr/local/bin/handoff-server-proc", "-d",
               "%s" % handoff_recv_datafile]
        LOG.debug("subprocess: %s" % cmd)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, close_fds=True)
        stdout_buf = StringIO.StringIO()

        def _wait_for_handoff_recv(print_log=True):
            """Called at an interval until VM synthesis finishes."""
            returncode = proc.poll()
            if returncode is None:
                # keep record stdout
                LOG.debug("waiting for finishing handoff recv")
                in_ready, _, _ = select.select([proc.stdout], [], [])
                try:
                    buf = os.read(proc.stdout.fileno(), 1024*100)
                    if print_log:
                        LOG.debug(buf)
                    stdout_buf.write(buf)
                except OSError as e:
                    if e.errno == errno.EAGAIN or e.errno == errno.EWOULDBLOCK:
                        return
            else:
                # handoff finishes. Read reamining stdout
                in_ready, _, _ = select.select([proc.stdout], [], [], 0.1)
                buf = proc.stdout.read()
                stdout_buf.write(buf)
                raise loopingcall.LoopingCallDone()
        timer = loopingcall.FixedIntervalLoopingCall(_wait_for_handoff_recv)
        timer.start(interval=0.5).wait()
        LOG.info("Handoff recv finishes")
        returncode = proc.poll()
        if returncode is not 0:
            msg = "Failed to receive handoff data"
            raise handoff.HandoffError(msg)

        # parse output: this will be fixed at cloudlet deamon
        keyword, disksize, memorysize, disk_overlay_map, memory_overlay_map =\
            stdout_buf.getvalue().split("\n")[-1].split("\t")
        if keyword.lower() != "openstack":
            raise handoff.HandoffError("Failed to parse returned data")
        return disksize, memorysize, disk_overlay_map, memory_overlay_map

    def _handoff_launch_vm(self, libvirt_xml, base_diskpath, base_mempath,
                           launch_disk, launch_memory,
                           launch_disk_size, launch_memory_size,
                           disk_overlay_map, memory_overlay_map):
        # We told to FUSE that we have everything ready, so we need to wait
        # until delta_proc fininshes. we cannot start VM before delta_proc
        # finishes, because we don't know what will be modified in the future
        fuse = synthesis.run_fuse(
            Cloudlet_Const.CLOUDLETFS_PATH, Cloudlet_Const.CHUNK_SIZE,
            base_diskpath, launch_disk_size, base_mempath, launch_memory_size,
            resumed_disk=launch_disk,  disk_overlay_map=disk_overlay_map,
            resumed_memory=launch_memory, memory_overlay_map=memory_overlay_map,
            valid_bit=1
        )
        synthesized_vm = synthesis.SynthesizedVM(
            launch_disk, launch_memory, fuse,
            disk_only=False, qemu_args=None,
            nova_xml=libvirt_xml,
            nova_conn=self._conn,
            nova_util=libvirt_utils
        )

        synthesized_vm.resume()
        return synthesized_vm
