# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
# Copyright (c) 2010 Citrix Systems, Inc.
# Copyright (c) 2011 Piston Cloud Computing, Inc
# Copyright (c) 2012 University Of Minho
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
A connection to a hypervisor through libvirt.

Supports KVM, LXC, QEMU, UML, and XEN.

**Related Flags**

:libvirt_type:  Libvirt domain type.  Can be kvm, qemu, uml, xen
                (default: kvm).
:libvirt_uri:  Override for the default libvirt URI (depends on libvirt_type).
:libvirt_disk_prefix:  Override the default disk prefix for the devices
                       attached to a server.
:rescue_image_id:  Rescue ami image (None = original image).
:rescue_kernel_id:  Rescue aki image (None = original image).
:rescue_ramdisk_id:  Rescue ari image (None = original image).
:injected_network_template:  Template file for injected network
:allow_same_net_traffic:  Whether to allow in project network traffic

"""

import errno
import functools
import glob
import hashlib
import multiprocessing
import os
import shutil
import sys
import uuid

from eventlet import greenthread
from eventlet import tpool
from lxml import etree
from xml.dom import minidom

from nova import block_device
from nova.compute import instance_types
from nova.compute import power_state
from nova import context as nova_context
from nova import db
from nova import exception
from nova import flags
from nova.image import glance
from nova.openstack.common import cfg
from nova.openstack.common import excutils
from nova.openstack.common import importutils
from nova.openstack.common import jsonutils
from nova.openstack.common import log as logging
from nova import utils
from nova.virt.disk import api as disk
from nova.virt import driver
from nova.virt.libvirt import config
from nova.virt.libvirt import firewall
from nova.virt.libvirt import imagebackend
from nova.virt.libvirt import imagecache
from nova.virt.libvirt import utils as libvirt_utils

libvirt = None
Template = None

LOG = logging.getLogger(__name__)

libvirt_opts = [
    cfg.StrOpt('rescue_image_id',
               default=None,
               help='Rescue ami image'),
    cfg.StrOpt('rescue_kernel_id',
               default=None,
               help='Rescue aki image'),
    cfg.StrOpt('rescue_ramdisk_id',
               default=None,
               help='Rescue ari image'),
    cfg.StrOpt('libvirt_type',
               default='kvm',
               help='Libvirt domain type (valid options are: '
                    'kvm, lxc, qemu, uml, xen)'),
    cfg.StrOpt('libvirt_uri',
               default='',
               help='Override the default libvirt URI '
                    '(which is dependent on libvirt_type)'),
    cfg.BoolOpt('libvirt_inject_password',
                default=False,
                help='Inject the admin password at boot time, '
                     'without an agent.'),
    cfg.BoolOpt('libvirt_inject_key',
                default=True,
                help='Inject the ssh public key at boot time'),
    cfg.IntOpt('libvirt_inject_partition',
                default=1,
                help='The partition to inject to : '
                     '-1 => inspect (libguestfs only), 0 => not partitioned, '
                     '>0 => partition number'),
    cfg.BoolOpt('use_usb_tablet',
                default=True,
                help='Sync virtual and real mouse cursors in Windows VMs'),
    cfg.StrOpt('live_migration_uri',
               default="qemu+tcp://%s/system",
               help='Migration target URI '
                    '(any included "%s" is replaced with '
                    'the migration target hostname)'),
    cfg.StrOpt('live_migration_flag',
               default='VIR_MIGRATE_UNDEFINE_SOURCE, VIR_MIGRATE_PEER2PEER',
               help='Migration flags to be set for live migration'),
    cfg.StrOpt('block_migration_flag',
               default='VIR_MIGRATE_UNDEFINE_SOURCE, VIR_MIGRATE_PEER2PEER, '
                       'VIR_MIGRATE_NON_SHARED_INC',
               help='Migration flags to be set for block migration'),
    cfg.IntOpt('live_migration_bandwidth',
               default=0,
               help='Maximum bandwidth to be used during migration, in Mbps'),
    cfg.StrOpt('snapshot_image_format',
               default=None,
               help='Snapshot image format (valid options are : '
                    'raw, qcow2, vmdk, vdi). '
                    'Defaults to same as source image'),
    cfg.StrOpt('libvirt_vif_driver',
               default='nova.virt.libvirt.vif.LibvirtBridgeDriver',
               help='The libvirt VIF driver to configure the VIFs.'),
    cfg.ListOpt('libvirt_volume_drivers',
                default=[
                  'iscsi=nova.virt.libvirt.volume.LibvirtISCSIVolumeDriver',
                  'local=nova.virt.libvirt.volume.LibvirtVolumeDriver',
                  'fake=nova.virt.libvirt.volume.LibvirtFakeVolumeDriver',
                  'rbd=nova.virt.libvirt.volume.LibvirtNetVolumeDriver',
                  'sheepdog=nova.virt.libvirt.volume.LibvirtNetVolumeDriver'
                  ],
                help='Libvirt handlers for remote volumes.'),
    cfg.StrOpt('libvirt_disk_prefix',
               default=None,
               help='Override the default disk prefix for the devices attached'
                    ' to a server, which is dependent on libvirt_type. '
                    '(valid options are: sd, xvd, uvd, vd)'),
    cfg.IntOpt('libvirt_wait_soft_reboot_seconds',
               default=120,
               help='Number of seconds to wait for instance to shut down after'
                    ' soft reboot request is made. We fall back to hard reboot'
                    ' if instance does not shutdown within this window.'),
    cfg.BoolOpt('libvirt_nonblocking',
                default=True,
                help='Use a separated OS thread pool to realize non-blocking'
                     ' libvirt calls'),
    # force_config_drive is a string option, to allow for future behaviors
    #  (e.g. use config_drive based on image properties)
    cfg.StrOpt('force_config_drive',
               default=None,
               help='Set to force injection to take place on a config drive '
                    '(if set, valid options are: always)'),
    cfg.StrOpt('libvirt_cpu_mode',
               default=None,
               help='Set to "host-model" to clone the host CPU feature flags; '
                    'to "host-passthrough" to use the host CPU model exactly; '
                    'to "custom" to use a named CPU model; '
                    'to "none" to not set any CPU model. '
                    'If libvirt_type="kvm|qemu", it will default to '
                    '"host-model", otherwise it will default to "none"'),
    cfg.StrOpt('libvirt_cpu_model',
               default=None,
               help='Set to a named libvirt CPU model (see names listed '
                    'in /usr/share/libvirt/cpu_map.xml). Only has effect if '
                    'libvirt_cpu_mode="custom" and libvirt_type="kvm|qemu"'),
    cfg.StrOpt('libvirt_snapshots_directory',
               default='$instances_path/snapshots',
               help='Location where libvirt driver will store snapshots '
                    'before uploading them to image service'),
    ]

FLAGS = flags.FLAGS
FLAGS.register_opts(libvirt_opts)

flags.DECLARE('live_migration_retry_count', 'nova.compute.manager')
flags.DECLARE('vncserver_proxyclient_address', 'nova.vnc')


def patch_tpool_proxy():
    """eventlet.tpool.Proxy doesn't work with old-style class in __str__()
    or __repr__() calls. See bug #962840 for details.
    We perform a monkey patch to replace those two instance methods.
    """
    def str_method(self):
        return str(self._obj)

    def repr_method(self):
        return repr(self._obj)

    tpool.Proxy.__str__ = str_method
    tpool.Proxy.__repr__ = repr_method


patch_tpool_proxy()

VIR_DOMAIN_NOSTATE = 0
VIR_DOMAIN_RUNNING = 1
VIR_DOMAIN_BLOCKED = 2
VIR_DOMAIN_PAUSED = 3
VIR_DOMAIN_SHUTDOWN = 4
VIR_DOMAIN_SHUTOFF = 5
VIR_DOMAIN_CRASHED = 6
VIR_DOMAIN_PMSUSPENDED = 7

LIBVIRT_POWER_STATE = {
    VIR_DOMAIN_NOSTATE: power_state.NOSTATE,
    VIR_DOMAIN_RUNNING: power_state.RUNNING,
    # NOTE(maoy): The DOMAIN_BLOCKED state is only valid in Xen.
    # It means that the VM is running and the vCPU is idle. So,
    # we map it to RUNNING
    VIR_DOMAIN_BLOCKED: power_state.RUNNING,
    VIR_DOMAIN_PAUSED: power_state.PAUSED,
    # NOTE(maoy): The libvirt API doc says that DOMAIN_SHUTDOWN
    # means the domain is being shut down. So technically the domain
    # is still running. SHUTOFF is the real powered off state.
    # But we will map both to SHUTDOWN anyway.
    # http://libvirt.org/html/libvirt-libvirt.html
    VIR_DOMAIN_SHUTDOWN: power_state.SHUTDOWN,
    VIR_DOMAIN_SHUTOFF: power_state.SHUTDOWN,
    VIR_DOMAIN_CRASHED: power_state.CRASHED,
    VIR_DOMAIN_PMSUSPENDED: power_state.SUSPENDED,
}

MIN_LIBVIRT_VERSION = (0, 9, 6)
# When the above version matches/exceeds this version
# delete it & corresponding code using it
MIN_LIBVIRT_HOST_CPU_VERSION = (0, 9, 10)


def _late_load_cheetah():
    global Template
    if Template is None:
        t = __import__('Cheetah.Template', globals(), locals(),
                       ['Template'], -1)
        Template = t.Template


def _get_eph_disk(ephemeral):
    return 'disk.eph' + str(ephemeral['num'])


class LibvirtDriver(driver.ComputeDriver):

    def __init__(self, read_only=False):
        super(LibvirtDriver, self).__init__()

        global libvirt
        if libvirt is None:
            libvirt = __import__('libvirt')

        _late_load_cheetah()

        self._host_state = None
        self._initiator = None
        self._wrapped_conn = None
        self.container = None
        self.read_only = read_only
        if FLAGS.firewall_driver not in firewall.drivers:
            FLAGS.set_default('firewall_driver', firewall.drivers[0])
        fw_class = importutils.import_class(FLAGS.firewall_driver)
        self.firewall_driver = fw_class(get_connection=self._get_connection)
        self.vif_driver = importutils.import_object(FLAGS.libvirt_vif_driver)
        self.volume_drivers = {}
        for driver_str in FLAGS.libvirt_volume_drivers:
            driver_type, _sep, driver = driver_str.partition('=')
            driver_class = importutils.import_class(driver)
            self.volume_drivers[driver_type] = driver_class(self)
        self._host_state = None

        disk_prefix_map = {"lxc": "", "uml": "ubd", "xen": "sd"}
        if FLAGS.libvirt_disk_prefix:
            self._disk_prefix = FLAGS.libvirt_disk_prefix
        else:
            self._disk_prefix = disk_prefix_map.get(FLAGS.libvirt_type, 'vd')
        self.default_root_device = self._disk_prefix + 'a'
        self.default_second_device = self._disk_prefix + 'b'
        self.default_third_device = self._disk_prefix + 'c'
        self.default_last_device = self._disk_prefix + 'z'

        self._disk_cachemode = None
        self.image_cache_manager = imagecache.ImageCacheManager()
        self.image_backend = imagebackend.Backend(FLAGS.use_cow_images)

    @property
    def disk_cachemode(self):
        if self._disk_cachemode is None:
            # We prefer 'none' for consistent performance, host crash
            # safety & migration correctness by avoiding host page cache.
            # Some filesystems (eg GlusterFS via FUSE) don't support
            # O_DIRECT though. For those we fallback to 'writethrough'
            # which gives host crash safety, and is safe for migration
            # provided the filesystem is cache coherant (cluster filesystems
            # typically are, but things like NFS are not).
            self._disk_cachemode = "none"
            if not self._supports_direct_io(FLAGS.instances_path):
                self._disk_cachemode = "writethrough"
        return self._disk_cachemode

    @property
    def host_state(self):
        if not self._host_state:
            self._host_state = HostState(self.read_only)
        return self._host_state

    def has_min_version(self, ver):
        libvirt_version = self._conn.getLibVersion()

        def _munge_version(ver):
            return ver[0] * 1000000 + ver[1] * 1000 + ver[2]

        if libvirt_version < _munge_version(ver):
            return False

        return True

    def init_host(self, host):
        if not self.has_min_version(MIN_LIBVIRT_VERSION):
            major = MIN_LIBVIRT_VERSION[0]
            minor = MIN_LIBVIRT_VERSION[1]
            micro = MIN_LIBVIRT_VERSION[2]
            LOG.error(_('Nova requires libvirt version '
                        '%(major)i.%(minor)i.%(micro)i or greater.') %
                        locals())

    def _get_connection(self):
        if not self._wrapped_conn or not self._test_connection():
            LOG.debug(_('Connecting to libvirt: %s'), self.uri)
            if not FLAGS.libvirt_nonblocking:
                self._wrapped_conn = self._connect(self.uri,
                                               self.read_only)
            else:
                self._wrapped_conn = tpool.proxy_call(
                    (libvirt.virDomain, libvirt.virConnect),
                    self._connect, self.uri, self.read_only)

        return self._wrapped_conn

    _conn = property(_get_connection)

    def _test_connection(self):
        try:
            self._wrapped_conn.getCapabilities()
            return True
        except libvirt.libvirtError as e:
            if (e.get_error_code() == libvirt.VIR_ERR_SYSTEM_ERROR and
                e.get_error_domain() in (libvirt.VIR_FROM_REMOTE,
                                         libvirt.VIR_FROM_RPC)):
                LOG.debug(_('Connection to libvirt broke'))
                return False
            raise

    @property
    def uri(self):
        if FLAGS.libvirt_type == 'uml':
            uri = FLAGS.libvirt_uri or 'uml:///system'
        elif FLAGS.libvirt_type == 'xen':
            uri = FLAGS.libvirt_uri or 'xen:///'
        elif FLAGS.libvirt_type == 'lxc':
            uri = FLAGS.libvirt_uri or 'lxc:///'
        else:
            uri = FLAGS.libvirt_uri or 'qemu:///system'
        return uri

    @staticmethod
    def _connect(uri, read_only):
        auth = [[libvirt.VIR_CRED_AUTHNAME, libvirt.VIR_CRED_NOECHOPROMPT],
                'root',
                None]

        if read_only:
            return libvirt.openReadOnly(uri)
        else:
            return libvirt.openAuth(uri, auth, 0)

    def get_num_instances(self):
        """Efficient override of base instance_exists method."""
        return self._conn.numOfDomains()

    def instance_exists(self, instance_id):
        """Efficient override of base instance_exists method."""
        try:
            self._lookup_by_name(instance_id)
            return True
        except exception.NovaException:
            return False

    # TODO(Shrews): Remove when libvirt Bugzilla bug # 836647 is fixed.
    def list_instance_ids(self):
        if self._conn.numOfDomains() == 0:
            return []
        return self._conn.listDomainsID()

    def list_instances(self):
        return [self._conn.lookupByID(x).name()
                for x in self.list_instance_ids()
                if x != 0]  # We skip domains with ID 0 (hypervisors).

    @staticmethod
    def _map_to_instance_info(domain):
        """Gets info from a virsh domain object into an InstanceInfo"""

        # domain.info() returns a list of:
        #    state:       one of the state values (virDomainState)
        #    maxMemory:   the maximum memory used by the domain
        #    memory:      the current amount of memory used by the domain
        #    nbVirtCPU:   the number of virtual CPU
        #    puTime:      the time used by the domain in nanoseconds

        (state, _max_mem, _mem, _num_cpu, _cpu_time) = domain.info()
        state = LIBVIRT_POWER_STATE[state]

        name = domain.name()

        return driver.InstanceInfo(name, state)

    def list_instances_detail(self):
        infos = []
        for domain_id in self.list_instance_ids():
            domain = self._conn.lookupByID(domain_id)
            info = self._map_to_instance_info(domain)
            infos.append(info)
        return infos

    def plug_vifs(self, instance, network_info):
        """Plug VIFs into networks."""
        for (network, mapping) in network_info:
            self.vif_driver.plug(instance, (network, mapping))

    def unplug_vifs(self, instance, network_info):
        """Unplug VIFs from networks."""
        for (network, mapping) in network_info:
            self.vif_driver.unplug(instance, (network, mapping))

    def _destroy(self, instance):
        try:
            virt_dom = self._lookup_by_name(instance['name'])
        except exception.NotFound:
            virt_dom = None

        # If the instance is already terminated, we're still happy
        # Otherwise, destroy it
        if virt_dom is not None:
            try:
                virt_dom.destroy()
            except libvirt.libvirtError as e:
                is_okay = False
                errcode = e.get_error_code()
                if errcode == libvirt.VIR_ERR_OPERATION_INVALID:
                    # If the instance if already shut off, we get this:
                    # Code=55 Error=Requested operation is not valid:
                    # domain is not running
                    (state, _max_mem, _mem, _cpus, _t) = virt_dom.info()
                    state = LIBVIRT_POWER_STATE[state]
                    if state == power_state.SHUTDOWN:
                        is_okay = True

                if not is_okay:
                    LOG.error(_("Error from libvirt during destroy. "
                                  "Code=%(errcode)s Error=%(e)s") %
                                locals(), instance=instance)
                    raise

        def _wait_for_destroy():
            """Called at an interval until the VM is gone."""
            try:
                state = self.get_info(instance)['state']
            except exception.NotFound:
                LOG.error(_("During wait destroy, instance disappeared."),
                          instance=instance)
                raise utils.LoopingCallDone(False)

            if state == power_state.SHUTDOWN:
                LOG.info(_("Instance destroyed successfully."),
                         instance=instance)
                raise utils.LoopingCallDone(True)

        timer = utils.LoopingCall(_wait_for_destroy)
        return timer.start(interval=0.5).wait()

    def destroy(self, instance, network_info, block_device_info=None):
        self._destroy(instance)
        self._cleanup(instance, network_info, block_device_info)

    def _cleanup(self, instance, network_info, block_device_info):
        try:
            virt_dom = self._lookup_by_name(instance['name'])
        except exception.NotFound:
            virt_dom = None
        if virt_dom:
            try:
                # NOTE(derekh): we can switch to undefineFlags and
                # VIR_DOMAIN_UNDEFINE_MANAGED_SAVE once we require 0.9.4
                if virt_dom.hasManagedSaveImage(0):
                    virt_dom.managedSaveRemove(0)
            except libvirt.libvirtError as e:
                errcode = e.get_error_code()
                LOG.error(_("Error from libvirt during saved instance "
                              "removal. Code=%(errcode)s Error=%(e)s") %
                            locals(), instance=instance)
            try:
                # NOTE(justinsb): We remove the domain definition.
                virt_dom.undefine()
            except libvirt.libvirtError as e:
                errcode = e.get_error_code()
                LOG.error(_("Error from libvirt during undefine. "
                              "Code=%(errcode)s Error=%(e)s") %
                            locals(), instance=instance)
                raise

        self.unplug_vifs(instance, network_info)
        try:
            self.firewall_driver.unfilter_instance(instance,
                                                   network_info=network_info)
        except libvirt.libvirtError as e:
            errcode = e.get_error_code()
            LOG.error(_("Error from libvirt during unfilter. "
                          "Code=%(errcode)s Error=%(e)s") %
                        locals(), instance=instance)
            reason = "Error unfiltering instance."
            raise exception.InstanceTerminationFailure(reason=reason)

        # NOTE(vish): we disconnect from volumes regardless
        block_device_mapping = driver.block_device_info_get_mapping(
            block_device_info)
        for vol in block_device_mapping:
            connection_info = vol['connection_info']
            mount_device = vol['mount_device'].rpartition("/")[2]
            self.volume_driver_method('disconnect_volume',
                                      connection_info,
                                      mount_device)

        target = os.path.join(FLAGS.instances_path, instance['name'])
        LOG.info(_('Deleting instance files %(target)s') % locals(),
                 instance=instance)
        if FLAGS.libvirt_type == 'lxc':
            disk.destroy_container(self.container)
        if os.path.exists(target):
            shutil.rmtree(target)

        #NOTE(bfilippov): destroy all LVM disks for this instance
        self._cleanup_lvm(instance)

    def _cleanup_lvm(self, instance):
        """Delete all LVM disks for given instance object"""
        disks = self._lvm_disks(instance)
        if disks:
            libvirt_utils.remove_logical_volumes(*disks)

    def _lvm_disks(self, instance):
        """Returns all LVM disks for given instance object"""
        if FLAGS.libvirt_images_volume_group:
            vg = os.path.join('/dev', FLAGS.libvirt_images_volume_group)
            if not os.path.exists(vg):
                return []
            pattern = '%s_' % instance['name']

            def belongs_to_instance(disk):
                return disk.startswith(pattern)

            def fullpath(name):
                return os.path.join(vg, name)

            disk_names = filter(belongs_to_instance, os.listdir(vg))
            disks = map(fullpath, disk_names)
            return disks
        return []

    def get_volume_connector(self, instance):
        if not self._initiator:
            self._initiator = libvirt_utils.get_iscsi_initiator()
            if not self._initiator:
                LOG.warn(_('Could not determine iscsi initiator name'),
                         instance=instance)
        return {
            'ip': FLAGS.my_ip,
            'initiator': self._initiator,
            'host': FLAGS.host
        }

    def _cleanup_resize(self, instance):
        target = os.path.join(FLAGS.instances_path,
                              instance['name'] + "_resize")
        if os.path.exists(target):
            shutil.rmtree(target)

    def volume_driver_method(self, method_name, connection_info,
                             *args, **kwargs):
        driver_type = connection_info.get('driver_volume_type')
        if not driver_type in self.volume_drivers:
            raise exception.VolumeDriverNotFound(driver_type=driver_type)
        driver = self.volume_drivers[driver_type]
        method = getattr(driver, method_name)
        return method(connection_info, *args, **kwargs)

    @exception.wrap_exception()
    def attach_volume(self, connection_info, instance_name, mountpoint):
        virt_dom = self._lookup_by_name(instance_name)
        mount_device = mountpoint.rpartition("/")[2]
        conf = self.volume_driver_method('connect_volume',
                                         connection_info,
                                         mount_device)

        if FLAGS.libvirt_type == 'lxc':
            self._attach_lxc_volume(conf.to_xml(), virt_dom, instance_name)
        else:
            try:
                virt_dom.attachDevice(conf.to_xml())
            except Exception, ex:
                self.volume_driver_method('disconnect_volume',
                                           connection_info,
                                           mount_device)

                if isinstance(ex, libvirt.libvirtError):
                    errcode = ex.get_error_code()
                    if errcode == libvirt.VIR_ERR_OPERATION_FAILED:
                        raise exception.DeviceIsBusy(device=mount_device)
                raise

        # TODO(danms) once libvirt has support for LXC hotplug,
        # replace this re-define with use of the
        # VIR_DOMAIN_AFFECT_LIVE & VIR_DOMAIN_AFFECT_CONFIG flags with
        # attachDevice()
        domxml = virt_dom.XMLDesc(libvirt.VIR_DOMAIN_XML_SECURE)
        self._conn.defineXML(domxml)

    @staticmethod
    def _get_disk_xml(xml, device):
        """Returns the xml for the disk mounted at device"""
        try:
            doc = etree.fromstring(xml)
        except Exception:
            return None
        ret = doc.findall('./devices/disk')
        for node in ret:
            for child in node.getchildren():
                if child.tag == 'target':
                    if child.get('dev') == device:
                        return etree.tostring(node)

    @exception.wrap_exception()
    def detach_volume(self, connection_info, instance_name, mountpoint):
        mount_device = mountpoint.rpartition("/")[2]
        try:
            # NOTE(vish): This is called to cleanup volumes after live
            #             migration, so we should still logout even if
            #             the instance doesn't exist here anymore.
            virt_dom = self._lookup_by_name(instance_name)
            xml = self._get_disk_xml(virt_dom.XMLDesc(0), mount_device)
            if not xml:
                raise exception.DiskNotFound(location=mount_device)
            if FLAGS.libvirt_type == 'lxc':
                self._detach_lxc_volume(xml, virt_dom, instance_name)
            else:
                virt_dom.detachDevice(xml)
        finally:
            self.volume_driver_method('disconnect_volume',
                                      connection_info,
                                      mount_device)

        # TODO(danms) once libvirt has support for LXC hotplug,
        # replace this re-define with use of the
        # VIR_DOMAIN_AFFECT_LIVE & VIR_DOMAIN_AFFECT_CONFIG flags with
        # detachDevice()
        domxml = virt_dom.XMLDesc(libvirt.VIR_DOMAIN_XML_SECURE)
        self._conn.defineXML(domxml)

    @exception.wrap_exception()
    def _attach_lxc_volume(self, xml, virt_dom, instance_name):
        LOG.info(_('attaching LXC block device'))

        lxc_container_root = self.get_lxc_container_root(virt_dom)
        lxc_host_volume = self.get_lxc_host_device(xml)
        lxc_container_device = self.get_lxc_container_target(xml)
        lxc_container_target = "%s/%s" % (lxc_container_root,
                                          lxc_container_device)

        if lxc_container_target:
            disk.bind(lxc_host_volume, lxc_container_target, instance_name)

    @exception.wrap_exception()
    def _detach_lxc_volume(self, xml, virt_dom, instance_name):
        LOG.info(_('detaching LXC block device'))

        lxc_container_root = self.get_lxc_container_root(virt_dom)
        lxc_container_device = self.get_lxc_container_target(xml)
        lxc_container_target = "%s/%s" % (lxc_container_root,
                                          lxc_container_device)

        if lxc_container_target:
            disk.unbind(lxc_container_target)

    @staticmethod
    def get_lxc_container_root(virt_dom):
        xml = virt_dom.XMLDesc(0)
        doc = etree.fromstring(xml)
        filesystem_block = doc.findall('./devices/filesystem')
        for cnt, filesystem_nodes in enumerate(filesystem_block):
            return filesystem_nodes[cnt].get('dir')

    @staticmethod
    def get_lxc_host_device(xml):
        dom = minidom.parseString(xml)

        for device in dom.getElementsByTagName('source'):
            return device.getAttribute('dev')

    @staticmethod
    def get_lxc_container_target(xml):
        dom = minidom.parseString(xml)

        for device in dom.getElementsByTagName('target'):
            filesystem = device.getAttribute('dev')
            return 'dev/%s' % filesystem

    @exception.wrap_exception()
    def snapshot(self, context, instance, image_href):
        """Create snapshot from a running VM instance.

        This command only works with qemu 0.14+
        """
        try:
            virt_dom = self._lookup_by_name(instance['name'])
        except exception.InstanceNotFound:
            raise exception.InstanceNotRunning()

        (image_service, image_id) = glance.get_remote_image_service(
            context, instance['image_ref'])
        try:
            base = image_service.show(context, image_id)
        except exception.ImageNotFound:
            base = {}

        _image_service = glance.get_remote_image_service(context, image_href)
        snapshot_image_service, snapshot_image_id = _image_service
        snapshot = snapshot_image_service.show(context, snapshot_image_id)

        metadata = {'is_public': False,
                    'status': 'active',
                    'name': snapshot['name'],
                    'properties': {
                                   'kernel_id': instance['kernel_id'],
                                   'image_location': 'snapshot',
                                   'image_state': 'available',
                                   'owner_id': instance['project_id'],
                                   'ramdisk_id': instance['ramdisk_id'],
                                   }
                    }
        if 'architecture' in base.get('properties', {}):
            arch = base['properties']['architecture']
            metadata['properties']['architecture'] = arch

        source_format = base.get('disk_format') or 'raw'
        if source_format == 'ami':
            # NOTE(vish): assume amis are raw
            source_format = 'raw'
        image_format = FLAGS.snapshot_image_format or source_format
        use_qcow2 = ((FLAGS.libvirt_images_type == 'default' and
                FLAGS.use_cow_images) or
                FLAGS.libvirt_images_type == 'qcow2')
        if use_qcow2:
            source_format = 'qcow2'
        # NOTE(vish): glance forces ami disk format to be ami
        if base.get('disk_format') == 'ami':
            metadata['disk_format'] = 'ami'
        else:
            metadata['disk_format'] = image_format

        if 'container_format' in base:
            metadata['container_format'] = base['container_format']

        # Find the disk
        xml_desc = virt_dom.XMLDesc(0)
        domain = etree.fromstring(xml_desc)
        source = domain.find('devices/disk/source')
        disk_path = source.get('file')

        snapshot_name = uuid.uuid4().hex

        (state, _max_mem, _mem, _cpus, _t) = virt_dom.info()
        state = LIBVIRT_POWER_STATE[state]

        if state == power_state.RUNNING:
            virt_dom.managedSave(0)
        # Make the snapshot
        libvirt_utils.create_snapshot(disk_path, snapshot_name)

        # Export the snapshot to a raw image
        snapshot_directory = FLAGS.libvirt_snapshots_directory
        libvirt_utils.ensure_tree(snapshot_directory)
        with utils.tempdir(dir=snapshot_directory) as tmpdir:
            try:
                out_path = os.path.join(tmpdir, snapshot_name)
                libvirt_utils.extract_snapshot(disk_path, source_format,
                                               snapshot_name, out_path,
                                               image_format)
            finally:
                libvirt_utils.delete_snapshot(disk_path, snapshot_name)
                if state == power_state.RUNNING:
                    self._create_domain(domain=virt_dom)

            # Upload that image to the image service
            with libvirt_utils.file_open(out_path) as image_file:
                image_service.update(context,
                                     image_href,
                                     metadata,
                                     image_file)

    @exception.wrap_exception()
    def reboot(self, instance, network_info, reboot_type='SOFT'):
        """Reboot a virtual machine, given an instance reference."""
        if reboot_type == 'SOFT':
            # NOTE(vish): This will attempt to do a graceful shutdown/restart.
            if self._soft_reboot(instance):
                LOG.info(_("Instance soft rebooted successfully."),
                         instance=instance)
                return
            else:
                LOG.warn(_("Failed to soft reboot instance."),
                         instance=instance)
        return self._hard_reboot(instance)

    def _soft_reboot(self, instance):
        """Attempt to shutdown and restart the instance gracefully.

        We use shutdown and create here so we can return if the guest
        responded and actually rebooted. Note that this method only
        succeeds if the guest responds to acpi. Therefore we return
        success or failure so we can fall back to a hard reboot if
        necessary.

        :returns: True if the reboot succeeded
        """
        dom = self._lookup_by_name(instance["name"])
        (state, _max_mem, _mem, _cpus, _t) = dom.info()
        state = LIBVIRT_POWER_STATE[state]
        # NOTE(vish): This check allows us to reboot an instance that
        #             is already shutdown.
        if state == power_state.RUNNING:
            dom.shutdown()
        # NOTE(vish): This actually could take slighty longer than the
        #             FLAG defines depending on how long the get_info
        #             call takes to return.
        for x in xrange(FLAGS.libvirt_wait_soft_reboot_seconds):
            (state, _max_mem, _mem, _cpus, _t) = dom.info()
            state = LIBVIRT_POWER_STATE[state]

            if state in [power_state.SHUTDOWN,
                         power_state.CRASHED]:
                LOG.info(_("Instance shutdown successfully."),
                         instance=instance)
                self._create_domain(domain=dom)
                timer = utils.LoopingCall(self._wait_for_running, instance)
                return timer.start(interval=0.5).wait()
            greenthread.sleep(1)
        return False

    def _hard_reboot(self, instance, xml=None):
        """Reboot a virtual machine, given an instance reference.

        Performs a Libvirt reset (if supported) on the domain.

        If Libvirt reset is unavailable this method actually destroys and
        re-creates the domain to ensure the reboot happens, as the guest
        OS cannot ignore this action.

        If xml is set, it uses the passed in xml in place of the xml from the
        existing domain.
        """

        virt_dom = self._lookup_by_name(instance['name'])
        # NOTE(itoumsn): Use XML delived from the running instance.
        if not xml:
            xml = virt_dom.XMLDesc(0)

        # NOTE(dprince): reset was added in Libvirt 0.9.7
        if hasattr(virt_dom, 'reset'):
            virt_dom.reset(0)
        else:
            self._destroy(instance)
            self._create_domain(xml, virt_dom)

        def _wait_for_reboot():
            """Called at an interval until the VM is running again."""
            try:
                state = self.get_info(instance)['state']
            except exception.NotFound:
                LOG.error(_("During reboot, instance disappeared."),
                          instance=instance)
                raise utils.LoopingCallDone

            if state == power_state.RUNNING:
                LOG.info(_("Instance rebooted successfully."),
                         instance=instance)
                raise utils.LoopingCallDone

        timer = utils.LoopingCall(_wait_for_reboot)
        return timer.start(interval=0.5).wait()

    @exception.wrap_exception()
    def pause(self, instance):
        """Pause VM instance"""
        dom = self._lookup_by_name(instance['name'])
        dom.suspend()

    @exception.wrap_exception()
    def unpause(self, instance):
        """Unpause paused VM instance"""
        dom = self._lookup_by_name(instance['name'])
        dom.resume()

    @exception.wrap_exception()
    def power_off(self, instance):
        """Power off the specified instance"""
        self._destroy(instance)

    @exception.wrap_exception()
    def power_on(self, instance):
        """Power on the specified instance"""
        dom = self._lookup_by_name(instance['name'])
        self._create_domain(domain=dom)
        timer = utils.LoopingCall(self._wait_for_running, instance)
        return timer.start(interval=0.5).wait()

    @exception.wrap_exception()
    def suspend(self, instance):
        """Suspend the specified instance"""
        dom = self._lookup_by_name(instance['name'])
        dom.managedSave(0)

    @exception.wrap_exception()
    def resume(self, instance):
        """resume the specified instance"""
        dom = self._lookup_by_name(instance['name'])
        self._create_domain(domain=dom)

    @exception.wrap_exception()
    def resume_state_on_host_boot(self, context, instance, network_info):
        """resume guest state when a host is booted"""
        virt_dom = self._lookup_by_name(instance['name'])
        xml = virt_dom.XMLDesc(0)
        self._create_domain_and_network(xml, instance, network_info)

    @exception.wrap_exception()
    def rescue(self, context, instance, network_info, image_meta):
        """Loads a VM using rescue images.

        A rescue is normally performed when something goes wrong with the
        primary images and data needs to be corrected/recovered. Rescuing
        should not edit or over-ride the original image, only allow for
        data recovery.

        """

        virt_dom = self._lookup_by_name(instance['name'])
        unrescue_xml = virt_dom.XMLDesc(0)
        unrescue_xml_path = os.path.join(FLAGS.instances_path,
                                         instance['name'],
                                         'unrescue.xml')
        libvirt_utils.write_to_file(unrescue_xml_path, unrescue_xml)

        rescue_images = {
            'image_id': FLAGS.rescue_image_id or instance['image_ref'],
            'kernel_id': FLAGS.rescue_kernel_id or instance['kernel_id'],
            'ramdisk_id': FLAGS.rescue_ramdisk_id or instance['ramdisk_id'],
        }
        xml = self.to_xml(instance, network_info, image_meta,
                          rescue=rescue_images)
        self._create_image(context, instance, xml, '.rescue', rescue_images,
                           network_info=network_info)
        self._destroy(instance)
        self._create_domain(xml, virt_dom)

    @exception.wrap_exception()
    def unrescue(self, instance, network_info):
        """Reboot the VM which is being rescued back into primary images.
        """
        unrescue_xml_path = os.path.join(FLAGS.instances_path,
                                         instance['name'],
                                         'unrescue.xml')
        xml = libvirt_utils.load_file(unrescue_xml_path)
        virt_dom = self._lookup_by_name(instance['name'])
        self._destroy(instance)
        self._create_domain(xml, virt_dom)
        libvirt_utils.file_delete(unrescue_xml_path)
        rescue_files = os.path.join(FLAGS.instances_path, instance['name'],
                                    "*.rescue")
        for rescue_file in glob.iglob(rescue_files):
            libvirt_utils.file_delete(rescue_file)

    @exception.wrap_exception()
    def poll_rebooting_instances(self, timeout):
        pass

    @exception.wrap_exception()
    def poll_rescued_instances(self, timeout):
        pass

    def _enable_hairpin(self, instance):
        interfaces = self.get_interfaces(instance['name'])
        for interface in interfaces:
            utils.execute('tee',
                          '/sys/class/net/%s/brport/hairpin_mode' % interface,
                          process_input='1',
                          run_as_root=True,
                          check_exit_code=[0, 1])

    # NOTE(ilyaalekseyev): Implementation like in multinics
    # for xenapi(tr3buchet)
    @exception.wrap_exception()
    def spawn(self, context, instance, image_meta, network_info,
              block_device_info=None):
        xml = self.to_xml(instance, network_info, image_meta,
                          block_device_info=block_device_info)
        self._create_image(context, instance, xml, network_info=network_info,
                           block_device_info=block_device_info)
        self._create_domain_and_network(xml, instance, network_info)
        LOG.debug(_("Instance is running"), instance=instance)

        def _wait_for_boot():
            """Called at an interval until the VM is running."""
            try:
                state = self.get_info(instance)['state']
            except exception.NotFound:
                LOG.error(_("During spawn, instance disappeared."),
                          instance=instance)
                raise utils.LoopingCallDone

            if state == power_state.RUNNING:
                LOG.info(_("Instance spawned successfully."),
                         instance=instance)
                raise utils.LoopingCallDone

        timer = utils.LoopingCall(_wait_for_boot)
        return timer.start(interval=0.5).wait()

    def _flush_libvirt_console(self, pty):
        out, err = utils.execute('dd',
                                 'if=%s' % pty,
                                 'iflag=nonblock',
                                 run_as_root=True,
                                 check_exit_code=False)
        return out

    def _append_to_file(self, data, fpath):
        LOG.info(_('data: %(data)r, fpath: %(fpath)r') % locals())
        fp = open(fpath, 'a+')
        fp.write(data)
        return fpath

    @exception.wrap_exception()
    def get_console_output(self, instance):
        virt_dom = self._lookup_by_name(instance['name'])
        xml = virt_dom.XMLDesc(0)
        tree = etree.fromstring(xml)

        console_types = {}

        # NOTE(comstud): We want to try 'file' types first, then try 'pty'
        # types.  We can't use Python 2.7 syntax of:
        # tree.find("./devices/console[@type='file']/source")
        # because we need to support 2.6.
        console_nodes = tree.findall('./devices/console')
        for console_node in console_nodes:
            console_type = console_node.get('type')
            console_types.setdefault(console_type, [])
            console_types[console_type].append(console_node)

        # If the guest has a console logging to a file prefer to use that
        if console_types.get('file'):
            for file_console in console_types.get('file'):
                source_node = file_console.find('./source')
                if source_node is None:
                    continue
                path = source_node.get("path")
                if not path:
                    continue
                libvirt_utils.chown(path, os.getuid())
                return libvirt_utils.load_file(path)

        # Try 'pty' types
        if console_types.get('pty'):
            for pty_console in console_types.get('pty'):
                source_node = pty_console.find('./source')
                if source_node is None:
                    continue
                pty = source_node.get("path")
                if not pty:
                    continue
                break
        else:
            msg = _("Guest does not have a console available")
            raise exception.NovaException(msg)

        self._chown_console_log_for_instance(instance['name'])
        data = self._flush_libvirt_console(pty)
        console_log = self._get_console_log_path(instance['name'])
        fpath = self._append_to_file(data, console_log)

        return libvirt_utils.load_file(fpath)

    @staticmethod
    def get_host_ip_addr():
        return FLAGS.my_ip

    @exception.wrap_exception()
    def get_vnc_console(self, instance):
        def get_vnc_port_for_instance(instance_name):
            virt_dom = self._lookup_by_name(instance_name)
            xml = virt_dom.XMLDesc(0)
            # TODO(sleepsonthefloor): use etree instead of minidom
            dom = minidom.parseString(xml)

            for graphic in dom.getElementsByTagName('graphics'):
                if graphic.getAttribute('type') == 'vnc':
                    return graphic.getAttribute('port')

        port = get_vnc_port_for_instance(instance['name'])
        host = FLAGS.vncserver_proxyclient_address

        return {'host': host, 'port': port, 'internal_access_path': None}

    @staticmethod
    def _supports_direct_io(dirpath):

        if not hasattr(os, 'O_DIRECT'):
            LOG.debug("This python runtime does not support direct I/O")
            return False

        testfile = os.path.join(dirpath, ".directio.test")

        hasDirectIO = True
        try:
            f = os.open(testfile, os.O_CREAT | os.O_WRONLY | os.O_DIRECT)
            os.close(f)
            LOG.debug(_("Path '%(path)s' supports direct I/O") %
                      {'path': dirpath})
        except OSError, e:
            if e.errno == errno.EINVAL:
                LOG.debug(_("Path '%(path)s' does not support direct I/O: "
                            "'%(ex)s'") % {'path': dirpath, 'ex': str(e)})
                hasDirectIO = False
            else:
                LOG.error(_("Error on '%(path)s' while checking direct I/O: "
                            "'%(ex)s'") % {'path': dirpath, 'ex': str(e)})
                raise e
        except Exception, e:
            LOG.error(_("Error on '%(path)s' while checking direct I/O: "
                        "'%(ex)s'") % {'path': dirpath, 'ex': str(e)})
            raise e
        finally:
            try:
                os.unlink(testfile)
            except Exception:
                pass

        return hasDirectIO

    @staticmethod
    def _create_local(target, local_size, unit='G',
                      fs_format=None, label=None):
        """Create a blank image of specified size"""

        if not fs_format:
            fs_format = FLAGS.default_ephemeral_format

        libvirt_utils.create_image('raw', target,
                                   '%d%c' % (local_size, unit))
        if fs_format:
            libvirt_utils.mkfs(fs_format, target, label)

    def _create_ephemeral(self, target, ephemeral_size, fs_label, os_type):
        self._create_local(target, ephemeral_size)
        disk.mkfs(os_type, fs_label, target)

    @staticmethod
    def _create_swap(target, swap_mb):
        """Create a swap file of specified size"""
        libvirt_utils.create_image('raw', target, '%dM' % swap_mb)
        libvirt_utils.mkfs('swap', target)

    @staticmethod
    def _get_console_log_path(instance_name):
        return os.path.join(FLAGS.instances_path, instance_name,
                'console.log')

    def _chown_console_log_for_instance(self, instance_name):
        console_log = self._get_console_log_path(instance_name)
        if os.path.exists(console_log):
            libvirt_utils.chown(console_log, os.getuid())

    def _create_image(self, context, instance, libvirt_xml, suffix='',
                      disk_images=None, network_info=None,
                      block_device_info=None):
        if not suffix:
            suffix = ''

        # syntactic nicety
        def basepath(fname='', suffix=suffix):
            return os.path.join(FLAGS.instances_path,
                                instance['name'],
                                fname + suffix)

        def image(fname, image_type=FLAGS.libvirt_images_type):
            return self.image_backend.image(instance['name'],
                                            fname, suffix, image_type)

        def raw(fname):
            return image(fname, image_type='raw')

        # ensure directories exist and are writable
        libvirt_utils.ensure_tree(basepath(suffix=''))

        LOG.info(_('Creating image'), instance=instance)
        libvirt_utils.write_to_file(basepath('libvirt.xml'), libvirt_xml)

        if FLAGS.libvirt_type == 'lxc':
            container_dir = '%s/rootfs' % basepath(suffix='')
            libvirt_utils.ensure_tree(container_dir)

        # NOTE(dprince): for rescue console.log may already exist... chown it.
        self._chown_console_log_for_instance(instance['name'])

        # NOTE(vish): No need add the suffix to console.log
        libvirt_utils.write_to_file(basepath('console.log', ''), '', 007)

        if not disk_images:
            disk_images = {'image_id': instance['image_ref'],
                           'kernel_id': instance['kernel_id'],
                           'ramdisk_id': instance['ramdisk_id']}

        if disk_images['kernel_id']:
            fname = disk_images['kernel_id']
            raw('kernel').cache(fn=libvirt_utils.fetch_image,
                                context=context,
                                fname=fname,
                                image_id=disk_images['kernel_id'],
                                user_id=instance['user_id'],
                                project_id=instance['project_id'])
            if disk_images['ramdisk_id']:
                fname = disk_images['ramdisk_id']
                raw('ramdisk').cache(fn=libvirt_utils.fetch_image,
                                     context=context,
                                     fname=fname,
                                     image_id=disk_images['ramdisk_id'],
                                     user_id=instance['user_id'],
                                     project_id=instance['project_id'])

        root_fname = hashlib.sha1(str(disk_images['image_id'])).hexdigest()
        size = instance['root_gb'] * 1024 * 1024 * 1024

        inst_type_id = instance['instance_type_id']
        inst_type = instance_types.get_instance_type(inst_type_id)
        if size == 0 or suffix == '.rescue':
            size = None

        if not self._volume_in_mapping(self.default_root_device,
                                       block_device_info):
            image('disk').cache(fn=libvirt_utils.fetch_image,
                                context=context,
                                fname=root_fname,
                                size=size,
                                image_id=disk_images['image_id'],
                                user_id=instance['user_id'],
                                project_id=instance['project_id'])

        ephemeral_gb = instance['ephemeral_gb']
        if ephemeral_gb and not self._volume_in_mapping(
                self.default_second_device, block_device_info):
            swap_device = self.default_third_device
            fn = functools.partial(self._create_ephemeral,
                                   fs_label='ephemeral0',
                                   os_type=instance["os_type"])
            fname = "ephemeral_%s_%s_%s" % ("0",
                                            ephemeral_gb,
                                            instance["os_type"])
            size = ephemeral_gb * 1024 * 1024 * 1024
            image('disk.local').cache(fn=fn,
                                      fname=fname,
                                      size=size,
                                      ephemeral_size=ephemeral_gb)
        else:
            swap_device = self.default_second_device

        for eph in driver.block_device_info_get_ephemerals(block_device_info):
            fn = functools.partial(self._create_ephemeral,
                                   fs_label='ephemeral%d' % eph['num'],
                                   os_type=instance["os_type"])
            size = eph['size'] * 1024 * 1024 * 1024
            fname = "ephemeral_%s_%s_%s" % (eph['num'],
                                            eph['size'],
                                            instance["os_type"])
            image(_get_eph_disk(eph)).cache(fn=fn,
                                            fname=fname,
                                            size=size,
                                            ephemeral_size=eph['size'])

        swap_mb = 0

        swap = driver.block_device_info_get_swap(block_device_info)
        if driver.swap_is_usable(swap):
            swap_mb = swap['swap_size']
        elif (inst_type['swap'] > 0 and
              not self._volume_in_mapping(swap_device, block_device_info)):
            swap_mb = inst_type['swap']

        if swap_mb > 0:
            size = swap_mb * 1024 * 1024
            image('disk.swap').cache(fn=self._create_swap,
                                     fname="swap_%s" % swap_mb,
                                     size=size,
                                     swap_mb=swap_mb)

        target_partition = None
        if not instance['kernel_id']:
            target_partition = FLAGS.libvirt_inject_partition
            if target_partition == 0:
                target_partition = None

        config_drive, config_drive_id = self._get_config_drive_info(instance)

        if any((FLAGS.libvirt_type == 'lxc', config_drive, config_drive_id)):
            target_partition = None

        if config_drive_id:
            fname = config_drive_id
            raw('disk.config').cache(fn=libvirt_utils.fetch_image,
                                     fname=fname,
                                     image_id=config_drive_id,
                                     user_id=instance['user_id'],
                                     project_id=instance['project_id'])
        elif config_drive:
            label = 'config'
            with utils.remove_path_on_error(basepath('disk.config')):
                self._create_local(basepath('disk.config'), 64, unit='M',
                                   fs_format='msdos', label=label)  # 64MB

        if FLAGS.libvirt_inject_key and instance['key_data']:
            key = str(instance['key_data'])
        else:
            key = None
        net = None

        nets = []
        ifc_template = open(FLAGS.injected_network_template).read()
        ifc_num = -1
        have_injected_networks = False
        for (network_ref, mapping) in network_info:
            ifc_num += 1

            if not network_ref['injected']:
                continue

            have_injected_networks = True
            address = mapping['ips'][0]['ip']
            netmask = mapping['ips'][0]['netmask']
            address_v6 = None
            gateway_v6 = None
            netmask_v6 = None
            if FLAGS.use_ipv6:
                address_v6 = mapping['ip6s'][0]['ip']
                netmask_v6 = mapping['ip6s'][0]['netmask']
                gateway_v6 = mapping['gateway_v6']
            net_info = {'name': 'eth%d' % ifc_num,
                   'address': address,
                   'netmask': netmask,
                   'gateway': mapping['gateway'],
                   'broadcast': mapping['broadcast'],
                   'dns': ' '.join(mapping['dns']),
                   'address_v6': address_v6,
                   'gateway_v6': gateway_v6,
                   'netmask_v6': netmask_v6}
            nets.append(net_info)

        if have_injected_networks:
            net = str(Template(ifc_template,
                               searchList=[{'interfaces': nets,
                                            'use_ipv6': FLAGS.use_ipv6}]))

        metadata = instance.get('metadata')

        if FLAGS.libvirt_inject_password:
            admin_pass = instance.get('admin_pass')
        else:
            admin_pass = None

        files = instance.get('injected_files')

        if any((key, net, metadata, admin_pass, files)):
            if config_drive:  # Should be True or None by now.
                injection_path = raw('disk.config').path
                img_id = 'config-drive'
            else:
                injection_path = image('disk').path
                img_id = instance.image_ref

            for injection in ('metadata', 'key', 'net', 'admin_pass', 'files'):
                if locals()[injection]:
                    LOG.info(_('Injecting %(injection)s into image'
                               ' %(img_id)s'), locals(), instance=instance)
            try:
                disk.inject_data(injection_path,
                                 key, net, metadata, admin_pass, files,
                                 partition=target_partition,
                                 use_cow=FLAGS.use_cow_images)

            except Exception as e:
                # This could be a windows image, or a vmdk format disk
                LOG.warn(_('Ignoring error injecting data into image '
                           '%(img_id)s (%(e)s)') % locals(),
                         instance=instance)

        if FLAGS.libvirt_type == 'lxc':
            self.container = disk.setup_container(basepath('disk'),
                                                  container_dir=container_dir,
                                                  use_cow=FLAGS.use_cow_images)

        if FLAGS.libvirt_type == 'uml':
            libvirt_utils.chown(basepath('disk'), 'root')

    @staticmethod
    def _volume_in_mapping(mount_device, block_device_info):
        block_device_list = [block_device.strip_dev(vol['mount_device'])
                             for vol in
                             driver.block_device_info_get_mapping(
                                 block_device_info)]
        swap = driver.block_device_info_get_swap(block_device_info)
        if driver.swap_is_usable(swap):
            block_device_list.append(
                block_device.strip_dev(swap['device_name']))
        block_device_list += [block_device.strip_dev(ephemeral['device_name'])
                              for ephemeral in
                              driver.block_device_info_get_ephemerals(
                                  block_device_info)]

        LOG.debug(_("block_device_list %s"), block_device_list)
        return block_device.strip_dev(mount_device) in block_device_list

    def _get_config_drive_info(self, instance):
        config_drive = instance.get('config_drive')
        config_drive_id = instance.get('config_drive_id')
        if FLAGS.force_config_drive:
            if not config_drive_id:
                config_drive = True
        return config_drive, config_drive_id

    def _has_config_drive(self, instance):
        config_drive, config_drive_id = self._get_config_drive_info(instance)
        return any((config_drive, config_drive_id))

    def get_host_capabilities(self):
        """Returns an instance of config.LibvirtConfigCaps representing
           the capabilities of the host"""
        xmlstr = self._conn.getCapabilities()

        caps = config.LibvirtConfigCaps()
        caps.parse_str(xmlstr)
        return caps

    def get_host_cpu_for_guest(self):
        """Returns an instance of config.LibvirtConfigGuestCPU
           representing the host's CPU model & topology with
           policy for configuring a guest to match"""

        caps = self.get_host_capabilities()
        hostcpu = caps.host.cpu
        guestcpu = config.LibvirtConfigGuestCPU()

        guestcpu.model = hostcpu.model
        guestcpu.vendor = hostcpu.vendor
        guestcpu.arch = hostcpu.arch

        guestcpu.match = "exact"

        for hostfeat in hostcpu.features:
            guestfeat = config.LibvirtConfigGuestCPUFeature(hostfeat.name)
            guestfeat.policy = "require"

        return guestcpu

    def get_guest_cpu_config(self):
        mode = FLAGS.libvirt_cpu_mode
        model = FLAGS.libvirt_cpu_model

        if mode is None:
            if FLAGS.libvirt_type == "kvm" or FLAGS.libvirt_type == "qemu":
                mode = "host-model"
            else:
                mode = "none"

        if mode == "none":
            return None

        if FLAGS.libvirt_type != "kvm" and FLAGS.libvirt_type != "qemu":
            msg = _("Config requested an explicit CPU model, but "
                    "the current libvirt hypervisor '%s' does not "
                    "support selecting CPU models") % FLAGS.libvirt_type
            raise exception.Invalid(msg)

        if mode == "custom" and model is None:
            msg = _("Config requested a custom CPU model, but no "
                    "model name was provided")
            raise exception.Invalid(msg)
        elif mode != "custom" and model is not None:
            msg = _("A CPU model name should not be set when a "
                    "host CPU model is requested")
            raise exception.Invalid(msg)

        LOG.debug(_("CPU mode '%(mode)s' model '%(model)s' was chosen")
                  % {'mode': mode, 'model': (model or "")})

        # TODO(berrange): in the future, when MIN_LIBVIRT_VERSION is
        # updated to be at least this new, we can kill off the elif
        # blocks here
        if self.has_min_version(MIN_LIBVIRT_HOST_CPU_VERSION):
            cpu = config.LibvirtConfigGuestCPU()
            cpu.mode = mode
            cpu.model = model
        elif mode == "custom":
            cpu = config.LibvirtConfigGuestCPU()
            cpu.model = model
        elif mode == "host-model":
            cpu = self.get_host_cpu_for_guest()
        elif mode == "host-passthrough":
            msg = _("Passthrough of the host CPU was requested but "
                    "this libvirt version does not support this feature")
            raise exception.NovaException(msg)

        return cpu

    def get_guest_config(self, instance, network_info, image_meta, rescue=None,
                         block_device_info=None):
        """Get config data for parameters.

        :param rescue: optional dictionary that should contain the key
            'ramdisk_id' if a ramdisk is needed for the rescue image and
            'kernel_id' if a kernel is needed for the rescue image.
        """
        block_device_mapping = driver.block_device_info_get_mapping(
            block_device_info)

        devs = []

        # FIXME(vish): stick this in db
        inst_type_id = instance['instance_type_id']
        inst_type = instance_types.get_instance_type(inst_type_id)

        guest = config.LibvirtConfigGuest()
        guest.virt_type = FLAGS.libvirt_type
        guest.name = instance['name']
        guest.uuid = instance['uuid']
        guest.memory = inst_type['memory_mb'] * 1024
        guest.vcpus = inst_type['vcpus']

        guest.cpu = self.get_guest_cpu_config()

        root_device_name = driver.block_device_info_get_root(block_device_info)
        if root_device_name:
            root_device = block_device.strip_dev(root_device_name)
        else:
            # NOTE(yamahata):
            # for nova.api.ec2.cloud.CloudController.get_metadata()
            root_device = self.default_root_device
            db.instance_update(
                nova_context.get_admin_context(), instance['uuid'],
                {'root_device_name': '/dev/' + self.default_root_device})

        if FLAGS.libvirt_type == "lxc":
            guest.os_type = "exe"
            guest.os_init_path = "/sbin/init"
            guest.os_cmdline = "console=ttyS0"
        elif FLAGS.libvirt_type == "uml":
            guest.os_type = "uml"
            guest.os_kernel = "/usr/bin/linux"
            guest.os_root = root_device_name or "/dev/ubda"
        else:
            if FLAGS.libvirt_type == "xen":
                guest.os_type = "linux"
                guest.os_root = root_device_name or "/dev/xvda"
            else:
                guest.os_type = "hvm"

            if rescue:
                if rescue.get('kernel_id'):
                    guest.os_kernel = os.path.join(FLAGS.instances_path,
                                                   instance['name'],
                                                   "kernel.rescue")
                if rescue.get('ramdisk_id'):
                    guest.os_initrd = os.path.join(FLAGS.instances_path,
                                                   instance['name'],
                                                   "ramdisk.rescue")
            elif instance['kernel_id']:
                guest.os_kernel = os.path.join(FLAGS.instances_path,
                                               instance['name'],
                                               "kernel")
                if FLAGS.libvirt_type == "xen":
                    guest.os_cmdline = "ro"
                else:
                    guest.os_cmdline = "root=%s console=ttyS0" % (
                        root_device_name or "/dev/vda",)
                if instance['ramdisk_id']:
                    guest.os_initrd = os.path.join(FLAGS.instances_path,
                                                   instance['name'],
                                                   "ramdisk")
            else:
                guest.os_boot_dev = "hd"

        if FLAGS.libvirt_type != "lxc" and FLAGS.libvirt_type != "uml":
            guest.acpi = True

        clk = config.LibvirtConfigGuestClock()
        clk.offset = "utc"
        guest.set_clock(clk)

        if FLAGS.libvirt_type == "kvm":
            # TODO(berrange) One day this should be per-guest
            # OS type configurable
            tmpit = config.LibvirtConfigGuestTimer()
            tmpit.name = "pit"
            tmpit.tickpolicy = "delay"

            tmrtc = config.LibvirtConfigGuestTimer()
            tmrtc.name = "rtc"
            tmrtc.tickpolicy = "catchup"

            clk.add_timer(tmpit)
            clk.add_timer(tmrtc)

        if FLAGS.libvirt_type == "lxc":
            fs = config.LibvirtConfigGuestFilesys()
            fs.source_type = "mount"
            fs.source_dir = os.path.join(FLAGS.instances_path,
                                         instance['name'],
                                         "rootfs")
            guest.add_device(fs)
        else:
            if image_meta and image_meta.get('disk_format') == 'iso':
                root_device_type = 'cdrom'
            else:
                root_device_type = 'disk'

            def disk_info(name, suffix=''):
                image = self.image_backend.image(instance['name'],
                                                 name, suffix)
                return image.libvirt_info(root_device_type)

            if FLAGS.libvirt_type == "uml":
                ephemeral_disk_bus = "uml"
            elif FLAGS.libvirt_type == "xen":
                ephemeral_disk_bus = "xen"
            else:
                ephemeral_disk_bus = "virtio"

            if rescue:
                diskrescue = disk_info('disk', '.rescue')
                diskrescue.driver_cache = self.disk_cachemode
                diskrescue.target_dev = self.default_root_device
                diskrescue.target_bus = ephemeral_disk_bus
                guest.add_device(diskrescue)

                diskos = disk_info('disk')
                diskos.driver_cache = self.disk_cachemode
                diskos.target_dev = self.default_second_device
                diskos.target_bus = ephemeral_disk_bus
                guest.add_device(diskos)
            else:
                ebs_root = self._volume_in_mapping(self.default_root_device,
                                                   block_device_info)

                if not ebs_root:
                    diskos = disk_info('disk')
                    diskos.driver_cache = self.disk_cachemode
                    diskos.target_dev = root_device
                    if root_device_type == "cdrom":
                        diskos.target_bus = "ide"
                        # NOTE(jk0): Only attach the ISO as a separate drive if
                        # the appropriate metadata is set on the image.
                        image_properties = image_meta.get("properties", {})
                        if "separate_attach" in image_properties:
                            separate_disk = disk_info("disk")
                            separate_disk.driver_cache = self.disk_cachemode
                            separate_disk.target_dev = self.default_root_device
                            separate_disk.target_bus = ephemeral_disk_bus
                            guest.add_device(separate_disk)
                    else:
                        diskos.target_bus = "virtio"
                    guest.add_device(diskos)

                ephemeral_device = None
                if not (self._volume_in_mapping(self.default_second_device,
                                                block_device_info) or
                        0 in [eph['num'] for eph in
                              driver.block_device_info_get_ephemerals(
                            block_device_info)]):
                    if instance['ephemeral_gb'] > 0:
                        ephemeral_device = self.default_second_device

                if ephemeral_device is not None:
                    disklocal = disk_info('disk.local')
                    disklocal.driver_cache = self.disk_cachemode
                    disklocal.target_dev = ephemeral_device
                    disklocal.target_bus = ephemeral_disk_bus
                    guest.add_device(disklocal)

                if ephemeral_device is not None:
                    swap_device = self.default_third_device
                    db.instance_update(
                        nova_context.get_admin_context(), instance['uuid'],
                        {'default_ephemeral_device':
                             '/dev/' + self.default_second_device})
                else:
                    swap_device = self.default_second_device

                for eph in driver.block_device_info_get_ephemerals(
                    block_device_info):
                    diskeph = disk_info(_get_eph_disk(eph))
                    diskeph.driver_cache = self.disk_cachemode
                    diskeph.target_dev = block_device.strip_dev(
                        eph['device_name'])
                    diskeph.target_bus = ephemeral_disk_bus
                    guest.add_device(diskeph)

                swap = driver.block_device_info_get_swap(block_device_info)
                if driver.swap_is_usable(swap):
                    diskswap = disk_info('disk.swap')
                    diskswap.driver_cache = self.disk_cachemode
                    diskswap.target_dev = block_device.strip_dev(
                        swap['device_name'])
                    diskswap.target_bus = ephemeral_disk_bus
                    guest.add_device(diskswap)
                elif (inst_type['swap'] > 0 and
                      not self._volume_in_mapping(swap_device,
                                                  block_device_info)):
                    diskswap = disk_info('disk.swap')
                    diskswap.driver_cache = self.disk_cachemode
                    diskswap.target_dev = swap_device
                    diskswap.target_bus = ephemeral_disk_bus
                    guest.add_device(diskswap)
                    db.instance_update(
                        nova_context.get_admin_context(), instance['uuid'],
                        {'default_swap_device': '/dev/' + swap_device})

                for vol in block_device_mapping:
                    connection_info = vol['connection_info']
                    mount_device = vol['mount_device'].rpartition("/")[2]
                    cfg = self.volume_driver_method('connect_volume',
                                                    connection_info,
                                                    mount_device)
                    guest.add_device(cfg)

            if self._has_config_drive(instance):
                diskconfig = config.LibvirtConfigGuestDisk()
                diskconfig.source_type = "file"
                diskconfig.driver_format = "raw"
                diskconfig.driver_cache = self.disk_cachemode
                diskconfig.source_path = os.path.join(FLAGS.instances_path,
                                                      instance['name'],
                                                      "disk.config")
                diskconfig.target_dev = self.default_last_device
                diskconfig.target_bus = ephemeral_disk_bus
                guest.add_device(diskconfig)

        for (network, mapping) in network_info:
            cfg = self.vif_driver.plug(instance, (network, mapping))
            guest.add_device(cfg)

        if FLAGS.libvirt_type == "qemu" or FLAGS.libvirt_type == "kvm":
            # The QEMU 'pty' driver throws away any data if no
            # client app is connected. Thus we can't get away
            # with a single type=pty console. Instead we have
            # to configure two separate consoles.
            consolelog = config.LibvirtConfigGuestSerial()
            consolelog.type = "file"
            consolelog.source_path = os.path.join(FLAGS.instances_path,
                                                  instance['name'],
                                                  "console.log")
            guest.add_device(consolelog)

            consolepty = config.LibvirtConfigGuestSerial()
            consolepty.type = "pty"
            guest.add_device(consolepty)
        else:
            consolepty = config.LibvirtConfigGuestConsole()
            consolepty.type = "pty"
            guest.add_device(consolepty)

        if FLAGS.vnc_enabled and FLAGS.libvirt_type not in ('lxc', 'uml'):
            if FLAGS.use_usb_tablet:
                tablet = config.LibvirtConfigGuestInput()
                tablet.type = "tablet"
                tablet.bus = "usb"
                guest.add_device(tablet)

            graphics = config.LibvirtConfigGuestGraphics()
            graphics.type = "vnc"
            graphics.keymap = FLAGS.vnc_keymap
            graphics.listen = FLAGS.vncserver_listen
            guest.add_device(graphics)

        return guest

    def to_xml(self, instance, network_info, image_meta=None, rescue=None,
               block_device_info=None):
        LOG.debug(_('Starting toXML method'), instance=instance)
        conf = self.get_guest_config(instance, network_info, image_meta,
                                     rescue, block_device_info)
        xml = conf.to_xml()
        LOG.debug(_('Finished toXML method'), instance=instance)
        return xml

    def _lookup_by_name(self, instance_name):
        """Retrieve libvirt domain object given an instance name.

        All libvirt error handling should be handled in this method and
        relevant nova exceptions should be raised in response.

        """
        try:
            return self._conn.lookupByName(instance_name)
        except libvirt.libvirtError as ex:
            error_code = ex.get_error_code()
            if error_code == libvirt.VIR_ERR_NO_DOMAIN:
                raise exception.InstanceNotFound(instance_id=instance_name)

            msg = _("Error from libvirt while looking up %(instance_name)s: "
                    "[Error Code %(error_code)s] %(ex)s") % locals()
            raise exception.NovaException(msg)

    def get_info(self, instance):
        """Retrieve information from libvirt for a specific instance name.

        If a libvirt error is encountered during lookup, we might raise a
        NotFound exception or Error exception depending on how severe the
        libvirt error is.

        """
        virt_dom = self._lookup_by_name(instance['name'])
        (state, max_mem, mem, num_cpu, cpu_time) = virt_dom.info()
        return {'state': LIBVIRT_POWER_STATE[state],
                'max_mem': max_mem,
                'mem': mem,
                'num_cpu': num_cpu,
                'cpu_time': cpu_time}

    def _create_domain(self, xml=None, domain=None, launch_flags=0):
        """Create a domain.

        Either domain or xml must be passed in. If both are passed, then
        the domain definition is overwritten from the xml.
        """
        if xml:
            domain = self._conn.defineXML(xml)
        domain.createWithFlags(launch_flags)
        return domain

    def _create_domain_and_network(self, xml, instance, network_info):
        """Do required network setup and create domain."""
        self.plug_vifs(instance, network_info)
        self.firewall_driver.setup_basic_filtering(instance, network_info)
        self.firewall_driver.prepare_instance_filter(instance, network_info)
        domain = self._create_domain(xml)
        self._enable_hairpin(instance)
        self.firewall_driver.apply_instance_filter(instance, network_info)
        return domain

    def get_all_block_devices(self):
        """
        Return all block devices in use on this node.
        """
        devices = []
        for dom_id in self.list_instance_ids():
            domain = self._conn.lookupByID(dom_id)
            try:
                doc = etree.fromstring(domain.XMLDesc(0))
            except Exception:
                continue
            ret = doc.findall('./devices/disk')
            for node in ret:
                if node.get('type') != 'block':
                    continue
                for child in node.getchildren():
                    if child.tag == 'source':
                        devices.append(child.get('dev'))
        return devices

    def get_disks(self, instance_name):
        """
        Note that this function takes an instance name.

        Returns a list of all block devices for this domain.
        """
        domain = self._lookup_by_name(instance_name)
        xml = domain.XMLDesc(0)

        try:
            doc = etree.fromstring(xml)
        except Exception:
            return []

        return filter(bool,
                      [target.get("dev")
                       for target in doc.findall('devices/disk/target')])

    def get_interfaces(self, instance_name):
        """
        Note that this function takes an instance name.

        Returns a list of all network interfaces for this instance.
        """
        domain = self._lookup_by_name(instance_name)
        xml = domain.XMLDesc(0)
        doc = None

        try:
            doc = etree.fromstring(xml)
        except Exception:
            return []

        interfaces = []

        ret = doc.findall('./devices/interface')

        for node in ret:
            devdst = None

            for child in list(node):
                if child.tag == 'target':
                    devdst = child.attrib['dev']

            if devdst is None:
                continue

            interfaces.append(devdst)

        return interfaces

    @staticmethod
    def get_vcpu_total():
        """Get vcpu number of physical computer.

        :returns: the number of cpu core.

        """

        # On certain platforms, this will raise a NotImplementedError.
        try:
            return multiprocessing.cpu_count()
        except NotImplementedError:
            LOG.warn(_("Cannot get the number of cpu, because this "
                       "function is not implemented for this platform. "
                       "This error can be safely ignored for now."))
            return 0

    def get_memory_mb_total(self):
        """Get the total memory size(MB) of physical computer.

        :returns: the total amount of memory(MB).

        """

        return self._conn.getInfo()[1]

    @staticmethod
    def get_local_gb_total():
        """Get the total hdd size(GB) of physical computer.

        :returns:
            The total amount of HDD(GB).
            Note that this value shows a partition where
            NOVA-INST-DIR/instances mounts.

        """

        stats = libvirt_utils.get_fs_info(FLAGS.instances_path)
        return stats['total'] / (1024 ** 3)

    def get_vcpu_used(self):
        """ Get vcpu usage number of physical computer.

        :returns: The total number of vcpu that currently used.

        """

        total = 0
        for dom_id in self.list_instance_ids():
            dom = self._conn.lookupByID(dom_id)
            vcpus = dom.vcpus()
            if vcpus is None:
                # dom.vcpus is not implemented for lxc, but returning 0 for
                # a used count is hardly useful for something measuring usage
                total += 1
            else:
                total += len(vcpus[1])
        return total

    def get_memory_mb_used(self):
        """Get the free memory size(MB) of physical computer.

        :returns: the total usage of memory(MB).

        """

        if sys.platform.upper() not in ['LINUX2', 'LINUX3']:
            return 0

        m = open('/proc/meminfo').read().split()
        idx1 = m.index('MemFree:')
        idx2 = m.index('Buffers:')
        idx3 = m.index('Cached:')
        if FLAGS.libvirt_type == 'xen':
            used = 0
            for domain_id in self.list_instance_ids():
                # skip dom0
                dom_mem = int(self._conn.lookupByID(domain_id).info()[2])
                if domain_id != 0:
                    used += dom_mem
                else:
                    # the mem reported by dom0 is be greater of what
                    # it is being used
                    used += (dom_mem -
                             (int(m[idx1 + 1]) +
                              int(m[idx2 + 1]) +
                              int(m[idx3 + 1])))
            # Convert it to MB
            return used / 1024
        else:
            avail = (int(m[idx1 + 1]) + int(m[idx2 + 1]) + int(m[idx3 + 1]))
            # Convert it to MB
            return self.get_memory_mb_total() - avail / 1024

    def get_local_gb_used(self):
        """Get the free hdd size(GB) of physical computer.

        :returns:
           The total usage of HDD(GB).
           Note that this value shows a partition where
           NOVA-INST-DIR/instances mounts.

        """

        stats = libvirt_utils.get_fs_info(FLAGS.instances_path)
        return stats['used'] / (1024 ** 3)

    def get_hypervisor_type(self):
        """Get hypervisor type.

        :returns: hypervisor type (ex. qemu)

        """

        return self._conn.getType()

    def get_hypervisor_version(self):
        """Get hypervisor version.

        :returns: hypervisor version (ex. 12003)

        """

        # NOTE(justinsb): getVersion moved between libvirt versions
        # Trying to do be compatible with older versions is a lost cause
        # But ... we can at least give the user a nice message
        method = getattr(self._conn, 'getVersion', None)
        if method is None:
            raise exception.NovaException(_("libvirt version is too old"
                                    " (does not support getVersion)"))
            # NOTE(justinsb): If we wanted to get the version, we could:
            # method = getattr(libvirt, 'getVersion', None)
            # NOTE(justinsb): This would then rely on a proper version check

        return method()

    def get_hypervisor_hostname(self):
        """Returns the hostname of the hypervisor."""
        return self._conn.getHostname()

    def get_cpu_info(self):
        """Get cpuinfo information.

        Obtains cpu feature from virConnect.getCapabilities,
        and returns as a json string.

        :return: see above description

        """

        caps = self.get_host_capabilities()
        cpu_info = dict()

        cpu_info['arch'] = caps.host.cpu.arch
        cpu_info['model'] = caps.host.cpu.model
        cpu_info['vendor'] = caps.host.cpu.vendor

        topology = dict()
        topology['sockets'] = caps.host.cpu.sockets
        topology['cores'] = caps.host.cpu.cores
        topology['threads'] = caps.host.cpu.threads
        cpu_info['topology'] = topology

        features = list()
        for f in caps.host.cpu.features:
            features.append(f.name)
        cpu_info['features'] = features

        guest_arches = list()
        for g in caps.guests:
            guest_arches.append(g.arch)
        cpu_info['permitted_instance_types'] = guest_arches

        # TODO(berrange): why do we bother converting the
        # libvirt capabilities XML into a special JSON format ?
        # The data format is different across all the drivers
        # so we could just return the raw capabilties XML
        # which 'compare_cpu' could use directly
        #
        # That said, arch_filter.py now seems to rely on
        # the libvirt drivers format which suggests this
        # data format needs to be standardized across drivers
        return jsonutils.dumps(cpu_info)

    def block_stats(self, instance_name, disk):
        """
        Note that this function takes an instance name.
        """
        domain = self._lookup_by_name(instance_name)
        return domain.blockStats(disk)

    def interface_stats(self, instance_name, interface):
        """
        Note that this function takes an instance name.
        """
        domain = self._lookup_by_name(instance_name)
        return domain.interfaceStats(interface)

    def get_console_pool_info(self, console_type):
        #TODO(mdragon): console proxy should be implemented for libvirt,
        #               in case someone wants to use it with kvm or
        #               such. For now return fake data.
        return {'address': '127.0.0.1',
                'username': 'fakeuser',
                'password': 'fakepassword'}

    def refresh_security_group_rules(self, security_group_id):
        self.firewall_driver.refresh_security_group_rules(security_group_id)

    def refresh_security_group_members(self, security_group_id):
        self.firewall_driver.refresh_security_group_members(security_group_id)

    def refresh_provider_fw_rules(self):
        self.firewall_driver.refresh_provider_fw_rules()

    def update_available_resource(self, ctxt, host):
        """Updates compute manager resource info on ComputeNode table.

        This method is called as a periodic task and is used only
        in live migration currently.

        :param ctxt: security context
        :param host: hostname that compute manager is currently running

        """

        try:
            service_ref = db.service_get_all_compute_by_host(ctxt, host)[0]
        except exception.NotFound:
            raise exception.ComputeServiceUnavailable(host=host)

        # Updating host information
        dic = {'vcpus': self.get_vcpu_total(),
               'memory_mb': self.get_memory_mb_total(),
               'local_gb': self.get_local_gb_total(),
               'vcpus_used': self.get_vcpu_used(),
               'memory_mb_used': self.get_memory_mb_used(),
               'local_gb_used': self.get_local_gb_used(),
               'hypervisor_type': self.get_hypervisor_type(),
               'hypervisor_version': self.get_hypervisor_version(),
               'hypervisor_hostname': self.get_hypervisor_hostname(),
               'cpu_info': self.get_cpu_info(),
               'service_id': service_ref['id'],
               'disk_available_least': self.get_disk_available_least()}

        compute_node_ref = service_ref['compute_node']
        if not compute_node_ref:
            LOG.info(_('Compute_service record created for %s ') % host)
            db.compute_node_create(ctxt, dic)
        else:
            LOG.info(_('Compute_service record updated for %s ') % host)
            db.compute_node_update(ctxt, compute_node_ref[0]['id'], dic)

    def check_can_live_migrate_destination(self, ctxt, instance_ref,
                                           block_migration=False,
                                           disk_over_commit=False):
        """Check if it is possible to execute live migration.

        This runs checks on the destination host, and then calls
        back to the source host to check the results.

        :param ctxt: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance
        :param dest: destination host
        :param block_migration: if true, prepare for block migration
        :param disk_over_commit: if true, allow disk over commit
        """
        if block_migration:
            self._assert_compute_node_has_enough_disk(ctxt,
                                                     instance_ref,
                                                     disk_over_commit)
        # Compare CPU
        src = instance_ref['host']
        source_cpu_info = self._get_compute_info(ctxt, src)['cpu_info']
        self._compare_cpu(source_cpu_info)

        # Create file on storage, to be checked on source host
        filename = self._create_shared_storage_test_file()

        return {"filename": filename, "block_migration": block_migration}

    def check_can_live_migrate_destination_cleanup(self, ctxt,
                                                   dest_check_data):
        """Do required cleanup on dest host after check_can_live_migrate calls

        :param ctxt: security context
        :param disk_over_commit: if true, allow disk over commit
        """
        filename = dest_check_data["filename"]
        self._cleanup_shared_storage_test_file(filename)

    def check_can_live_migrate_source(self, ctxt, instance_ref,
                                      dest_check_data):
        """Check if it is possible to execute live migration.

        This checks if the live migration can succeed, based on the
        results from check_can_live_migrate_destination.

        :param context: security context
        :param instance_ref: nova.db.sqlalchemy.models.Instance
        :param dest_check_data: result of check_can_live_migrate_destination
        """
        # Checking shared storage connectivity
        # if block migration, instances_paths should not be on shared storage.
        dest = FLAGS.host
        filename = dest_check_data["filename"]
        block_migration = dest_check_data["block_migration"]

        shared = self._check_shared_storage_test_file(filename)

        if block_migration:
            if shared:
                reason = _("Block migration can not be used "
                           "with shared storage.")
                raise exception.InvalidSharedStorage(reason=reason, path=dest)

        elif not shared:
            reason = _("Live migration can not be used "
                       "without shared storage.")
            raise exception.InvalidSharedStorage(reason=reason, path=dest)

    def _get_compute_info(self, context, host):
        """Get compute host's information specified by key"""
        compute_node_ref = db.service_get_all_compute_by_host(context, host)
        return compute_node_ref[0]['compute_node'][0]

    def _assert_compute_node_has_enough_disk(self, context, instance_ref,
                                             disk_over_commit):
        """Checks if host has enough disk for block migration."""
        # Libvirt supports qcow2 disk format,which is usually compressed
        # on compute nodes.
        # Real disk image (compressed) may enlarged to "virtual disk size",
        # that is specified as the maximum disk size.
        # (See qemu-img -f path-to-disk)
        # Scheduler recognizes destination host still has enough disk space
        # if real disk size < available disk size
        # if disk_over_commit is True,
        #  otherwise virtual disk size < available disk size.

        # Getting total available disk of host
        dest = FLAGS.host
        available_gb = self._get_compute_info(context,
                                              dest)['disk_available_least']
        available = available_gb * (1024 ** 3)

        ret = self.get_instance_disk_info(instance_ref['name'])
        disk_infos = jsonutils.loads(ret)

        necessary = 0
        if disk_over_commit:
            for info in disk_infos:
                necessary += int(info['disk_size'])
        else:
            for info in disk_infos:
                necessary += int(info['virt_disk_size'])

        # Check that available disk > necessary disk
        if (available - necessary) < 0:
            instance_uuid = instance_ref['uuid']
            reason = _("Unable to migrate %(instance_uuid)s to %(dest)s: "
                       "Lack of disk(host:%(available)s "
                       "<= instance:%(necessary)s)")
            raise exception.MigrationError(reason=reason % locals())

    def _compare_cpu(self, cpu_info):
        """Checks the host cpu is compatible to a cpu given by xml.

        "xml" must be a part of libvirt.openReadonly().getCapabilities().
        return values follows by virCPUCompareResult.
        if 0 > return value, do live migration.
        'http://libvirt.org/html/libvirt-libvirt.html#virCPUCompareResult'

        :param cpu_info: json string that shows cpu feature(see get_cpu_info())
        :returns:
            None. if given cpu info is not compatible to this server,
            raise exception.
        """
        info = jsonutils.loads(cpu_info)
        LOG.info(_('Instance launched has CPU info:\n%s') % cpu_info)
        cpu = config.LibvirtConfigCPU()
        cpu.arch = info['arch']
        cpu.model = info['model']
        cpu.vendor = info['vendor']
        cpu.sockets = info['topology']['sockets']
        cpu.cores = info['topology']['cores']
        cpu.threads = info['topology']['threads']
        for f in info['features']:
            cpu.add_feature(config.LibvirtConfigCPUFeature(f))

        u = "http://libvirt.org/html/libvirt-libvirt.html#virCPUCompareResult"
        m = _("CPU doesn't have compatibility.\n\n%(ret)s\n\nRefer to %(u)s")
        # unknown character exists in xml, then libvirt complains
        try:
            ret = self._conn.compareCPU(cpu.to_xml(), 0)
        except libvirt.libvirtError, e:
            ret = e.message
            LOG.error(m % locals())
            raise

        if ret <= 0:
            LOG.error(reason=m % locals())
            raise exception.InvalidCPUInfo(reason=m % locals())

    def _create_shared_storage_test_file(self):
        """Makes tmpfile under FLAGS.instance_path."""
        dirpath = FLAGS.instances_path
        fd, tmp_file = tempfile.mkstemp(dir=dirpath)
        LOG.debug(_("Creating tmpfile %s to notify to other "
                    "compute nodes that they should mount "
                    "the same storage.") % tmp_file)
        os.close(fd)
        return os.path.basename(tmp_file)

    def _check_shared_storage_test_file(self, filename):
        """Confirms existence of the tmpfile under FLAGS.instances_path.
        Cannot confirm tmpfile return False."""
        tmp_file = os.path.join(FLAGS.instances_path, filename)
        if not os.path.exists(tmp_file):
            return False
        else:
            return True

    def _cleanup_shared_storage_test_file(self, filename):
        """Removes existence of the tmpfile under FLAGS.instances_path."""
        tmp_file = os.path.join(FLAGS.instances_path, filename)
        os.remove(tmp_file)

    def ensure_filtering_rules_for_instance(self, instance_ref, network_info,
                                            time=None):
        """Setting up filtering rules and waiting for its completion.

        To migrate an instance, filtering rules to hypervisors
        and firewalls are inevitable on destination host.
        ( Waiting only for filterling rules to hypervisor,
        since filtering rules to firewall rules can be set faster).

        Concretely, the below method must be called.
        - setup_basic_filtering (for nova-basic, etc.)
        - prepare_instance_filter(for nova-instance-instance-xxx, etc.)

        to_xml may have to be called since it defines PROJNET, PROJMASK.
        but libvirt migrates those value through migrateToURI(),
        so , no need to be called.

        Don't use thread for this method since migration should
        not be started when setting-up filtering rules operations
        are not completed.

        :params instance_ref: nova.db.sqlalchemy.models.Instance object

        """

        if not time:
            time = greenthread

        # If any instances never launch at destination host,
        # basic-filtering must be set here.
        self.firewall_driver.setup_basic_filtering(instance_ref, network_info)
        # setting up nova-instance-instance-xx mainly.
        self.firewall_driver.prepare_instance_filter(instance_ref,
                network_info)

        # wait for completion
        timeout_count = range(FLAGS.live_migration_retry_count)
        while timeout_count:
            if self.firewall_driver.instance_filter_exists(instance_ref,
                                                           network_info):
                break
            timeout_count.pop()
            if len(timeout_count) == 0:
                msg = _('Timeout migrating for %s. nwfilter not found.')
                raise exception.NovaException(msg % instance_ref["name"])
            time.sleep(1)

    def live_migration(self, ctxt, instance_ref, dest,
                       post_method, recover_method, block_migration=False):
        """Spawning live_migration operation for distributing high-load.

        :params ctxt: security context
        :params instance_ref:
            nova.db.sqlalchemy.models.Instance object
            instance object that is migrated.
        :params dest: destination host
        :params block_migration: destination host
        :params post_method:
            post operation method.
            expected nova.compute.manager.post_live_migration.
        :params recover_method:
            recovery method when any exception occurs.
            expected nova.compute.manager.recover_live_migration.
        :params block_migration: if true, do block migration.

        """

        greenthread.spawn(self._live_migration, ctxt, instance_ref, dest,
                          post_method, recover_method, block_migration)

    def _live_migration(self, ctxt, instance_ref, dest, post_method,
                        recover_method, block_migration=False):
        """Do live migration.

        :params ctxt: security context
        :params instance_ref:
            nova.db.sqlalchemy.models.Instance object
            instance object that is migrated.
        :params dest: destination host
        :params post_method:
            post operation method.
            expected nova.compute.manager.post_live_migration.
        :params recover_method:
            recovery method when any exception occurs.
            expected nova.compute.manager.recover_live_migration.

        """

        # Do live migration.
        try:
            if block_migration:
                flaglist = FLAGS.block_migration_flag.split(',')
            else:
                flaglist = FLAGS.live_migration_flag.split(',')
            flagvals = [getattr(libvirt, x.strip()) for x in flaglist]
            logical_sum = reduce(lambda x, y: x | y, flagvals)

            dom = self._lookup_by_name(instance_ref["name"])
            dom.migrateToURI(FLAGS.live_migration_uri % dest,
                             logical_sum,
                             None,
                             FLAGS.live_migration_bandwidth)

        except Exception:
            with excutils.save_and_reraise_exception():
                recover_method(ctxt, instance_ref, dest, block_migration)

        # Waiting for completion of live_migration.
        timer = utils.LoopingCall(f=None)

        def wait_for_live_migration():
            """waiting for live migration completion"""
            try:
                self.get_info(instance_ref)['state']
            except exception.NotFound:
                timer.stop()
                post_method(ctxt, instance_ref, dest, block_migration)

        timer.f = wait_for_live_migration
        return timer.start(interval=0.5).wait()

    def pre_live_migration(self, context, instance_ref, block_device_info,
                           network_info):
        """Preparation live migration."""
        # Establishing connection to volume server.
        block_device_mapping = driver.block_device_info_get_mapping(
            block_device_info)
        for vol in block_device_mapping:
            connection_info = vol['connection_info']
            mount_device = vol['mount_device'].rpartition("/")[2]
            self.volume_driver_method('connect_volume',
                                      connection_info,
                                      mount_device)

        # We call plug_vifs before the compute manager calls
        # ensure_filtering_rules_for_instance, to ensure bridge is set up
        # Retry operation is necessary because continuously request comes,
        # concorrent request occurs to iptables, then it complains.
        max_retry = FLAGS.live_migration_retry_count
        for cnt in range(max_retry):
            try:
                self.plug_vifs(instance_ref, network_info)
                break
            except exception.ProcessExecutionError:
                if cnt == max_retry - 1:
                    raise
                else:
                    LOG.warn(_("plug_vifs() failed %(cnt)d."
                               "Retry up to %(max_retry)d for %(hostname)s.")
                               % locals())
                    time.sleep(1)

    def pre_block_migration(self, ctxt, instance_ref, disk_info_json):
        """Preparation block migration.

        :params ctxt: security context
        :params instance_ref:
            nova.db.sqlalchemy.models.Instance object
            instance object that is migrated.
        :params disk_info_json:
            json strings specified in get_instance_disk_info

        """
        disk_info = jsonutils.loads(disk_info_json)

        # make instance directory
        instance_dir = os.path.join(FLAGS.instances_path, instance_ref['name'])
        if os.path.exists(instance_dir):
            raise exception.DestinationDiskExists(path=instance_dir)
        os.mkdir(instance_dir)

        for info in disk_info:
            base = os.path.basename(info['path'])
            # Get image type and create empty disk image, and
            # create backing file in case of qcow2.
            instance_disk = os.path.join(instance_dir, base)
            if not info['backing_file']:
                libvirt_utils.create_image(info['type'], instance_disk,
                                           info['disk_size'])
            else:
                # Creating backing file follows same way as spawning instances.
                cache_name = os.path.basename(info['backing_file'])
                # Remove any size tags which the cache manages
                cache_name = cache_name.split('_')[0]

                self._cache_image(fn=libvirt_utils.fetch_image,
                    context=ctxt,
                    target=instance_disk,
                    fname=cache_name,
                    cow=FLAGS.use_cow_images,
                    image_id=instance_ref['image_ref'],
                    user_id=instance_ref['user_id'],
                    project_id=instance_ref['project_id'],
                    size=info['virt_disk_size'])

        # if image has kernel and ramdisk, just download
        # following normal way.
        if instance_ref['kernel_id']:
            libvirt_utils.fetch_image(ctxt,
                              os.path.join(instance_dir, 'kernel'),
                              instance_ref['kernel_id'],
                              instance_ref['user_id'],
                              instance_ref['project_id'])
            if instance_ref['ramdisk_id']:
                libvirt_utils.fetch_image(ctxt,
                                  os.path.join(instance_dir, 'ramdisk'),
                                  instance_ref['ramdisk_id'],
                                  instance_ref['user_id'],
                                  instance_ref['project_id'])

    def post_live_migration_at_destination(self, ctxt,
                                           instance_ref,
                                           network_info,
                                           block_migration):
        """Post operation of live migration at destination host.

        :param ctxt: security context
        :param instance_ref:
            nova.db.sqlalchemy.models.Instance object
            instance object that is migrated.
        :param network_info: instance network infomation
        :param block_migration: if true, post operation of block_migraiton.
        """
        # Define migrated instance, otherwise, suspend/destroy does not work.
        dom_list = self._conn.listDefinedDomains()
        if instance_ref["name"] not in dom_list:
            instance_dir = os.path.join(FLAGS.instances_path,
                                        instance_ref["name"])
            xml_path = os.path.join(instance_dir, 'libvirt.xml')
            # In case of block migration, destination does not have
            # libvirt.xml
            if not os.path.isfile(xml_path):
                xml = self.to_xml(instance_ref, network_info=network_info)
                f = open(os.path.join(instance_dir, 'libvirt.xml'), 'w+')
                f.write(xml)
                f.close()
            # libvirt.xml should be made by to_xml(), but libvirt
            # does not accept to_xml() result, since uuid is not
            # included in to_xml() result.
            dom = self._lookup_by_name(instance_ref["name"])
            self._conn.defineXML(dom.XMLDesc(0))

    def get_instance_disk_info(self, instance_name):
        """Preparation block migration.

        :params ctxt: security context
        :params instance_ref:
            nova.db.sqlalchemy.models.Instance object
            instance object that is migrated.
        :return:
            json strings with below format::

                "[{'path':'disk', 'type':'raw',
                  'virt_disk_size':'10737418240',
                  'backing_file':'backing_file',
                  'disk_size':'83886080'},...]"

        """
        disk_info = []

        virt_dom = self._lookup_by_name(instance_name)
        xml = virt_dom.XMLDesc(0)
        doc = etree.fromstring(xml)
        disk_nodes = doc.findall('.//devices/disk')
        path_nodes = doc.findall('.//devices/disk/source')
        driver_nodes = doc.findall('.//devices/disk/driver')

        for cnt, path_node in enumerate(path_nodes):
            disk_type = disk_nodes[cnt].get('type')
            path = path_node.get('file')

            if disk_type != 'file':
                LOG.debug(_('skipping %(path)s since it looks like volume') %
                          locals())
                continue

            # get the real disk size or
            # raise a localized error if image is unavailable
            dk_size = int(os.path.getsize(path))

            disk_type = driver_nodes[cnt].get('type')
            if disk_type == "qcow2":
                out, err = utils.execute('qemu-img', 'info', path)

                # virtual size:
                size = [i.split('(')[1].split()[0] for i in out.split('\n')
                    if i.strip().find('virtual size') >= 0]
                virt_size = int(size[0])

                # backing file:(actual path:)
                backing_file = libvirt_utils.get_disk_backing_file(path)
            else:
                backing_file = ""
                virt_size = 0

            disk_info.append({'type': disk_type,
                              'path': path,
                              'virt_disk_size': virt_size,
                              'backing_file': backing_file,
                              'disk_size': dk_size})
        return jsonutils.dumps(disk_info)

    def get_disk_available_least(self):
        """Return disk available least size.

        The size of available disk, when block_migration command given
        disk_over_commit param is FALSE.

        The size that deducted real nstance disk size from the total size
        of the virtual disk of all instances.

        """
        # available size of the disk
        dk_sz_gb = self.get_local_gb_total() - self.get_local_gb_used()

        # Disk size that all instance uses : virtual_size - disk_size
        instances_name = self.list_instances()
        instances_sz = 0
        for i_name in instances_name:
            try:
                disk_infos = jsonutils.loads(
                        self.get_instance_disk_info(i_name))
                for info in disk_infos:
                    i_vt_sz = int(info['virt_disk_size'])
                    i_dk_sz = int(info['disk_size'])
                    instances_sz += i_vt_sz - i_dk_sz
            except OSError as e:
                if e.errno == errno.ENOENT:
                    LOG.error(_("Getting disk size of %(i_name)s: %(e)s") %
                              locals())
                else:
                    raise
            except exception.InstanceNotFound:
                # Instance was deleted during the check so ignore it
                pass

        # Disk available least size
        available_least_size = dk_sz_gb * (1024 ** 3) - instances_sz
        return (available_least_size / 1024 / 1024 / 1024)

    def unfilter_instance(self, instance_ref, network_info):
        """See comments of same method in firewall_driver."""
        self.firewall_driver.unfilter_instance(instance_ref,
                                               network_info=network_info)

    def update_host_status(self):
        """Retrieve status info from libvirt.

        Query libvirt to get the state of the compute node, such
        as memory and disk usage.
        """
        return self.host_state.update_status()

    def get_host_stats(self, refresh=False):
        """Return the current state of the host.

        If 'refresh' is True, run update the stats first."""
        return self.host_state.get_host_stats(refresh=refresh)

    def host_power_action(self, host, action):
        """Reboots, shuts down or powers up the host."""
        raise NotImplementedError()

    def host_maintenance_mode(self, host, mode):
        """Start/Stop host maintenance window. On start, it triggers
        guest VMs evacuation."""
        raise NotImplementedError()

    def set_host_enabled(self, host, enabled):
        """Sets the specified host's ability to accept new instances."""
        pass

    def manage_image_cache(self, context):
        """Manage the local cache of images."""
        self.image_cache_manager.verify_base_images(context)

    @exception.wrap_exception()
    def migrate_disk_and_power_off(self, context, instance, dest,
                                   instance_type, network_info):
        LOG.debug(_("Starting migrate_disk_and_power_off"),
                   instance=instance)
        disk_info_text = self.get_instance_disk_info(instance['name'])
        disk_info = jsonutils.loads(disk_info_text)

        self.power_off(instance)

        # copy disks to destination
        # if disk type is qcow2, convert to raw then send to dest.
        # rename instance dir to +_resize at first for using
        # shared storage for instance dir (eg. NFS).
        same_host = (dest == self.get_host_ip_addr())
        inst_base = "%s/%s" % (FLAGS.instances_path, instance['name'])
        inst_base_resize = inst_base + "_resize"
        try:
            utils.execute('mv', inst_base, inst_base_resize)
            if same_host:
                utils.execute('mkdir', '-p', inst_base)
            else:
                utils.execute('ssh', dest, 'mkdir', '-p', inst_base)
            for info in disk_info:
                # assume inst_base == dirname(info['path'])
                to_path = "%s:%s" % (dest, info['path'])
                fname = os.path.basename(info['path'])
                from_path = os.path.join(inst_base_resize, fname)
                if info['type'] == 'qcow2':
                    tmp_path = from_path + "_rbase"
                    utils.execute('qemu-img', 'convert', '-f', 'qcow2',
                                  '-O', 'raw', from_path, tmp_path)
                    if same_host:
                        utils.execute('mv', tmp_path, info['path'])
                    else:
                        utils.execute('scp', tmp_path, to_path)
                        utils.execute('rm', '-f', tmp_path)
                else:  # raw
                    if same_host:
                        utils.execute('cp', from_path, info['path'])
                    else:
                        utils.execute('scp', from_path, to_path)
        except Exception, e:
            try:
                if os.path.exists(inst_base_resize):
                    utils.execute('rm', '-rf', inst_base)
                    utils.execute('mv', inst_base_resize, inst_base)
                    utils.execute('ssh', dest, 'rm', '-rf', inst_base)
            except Exception:
                pass
            raise e

        return disk_info_text

    def _wait_for_running(self, instance):
        try:
            state = self.get_info(instance)['state']
        except exception.NotFound:
            LOG.error(_("During wait running, instance disappeared."),
                      instance=instance)
            raise utils.LoopingCallDone(False)

        if state == power_state.RUNNING:
            LOG.info(_("Instance running successfully."), instance=instance)
            raise utils.LoopingCallDone(True)

    @exception.wrap_exception()
    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance):
        LOG.debug(_("Starting finish_migration"), instance=instance)

        # resize disks. only "disk" and "disk.local" are necessary.
        disk_info = jsonutils.loads(disk_info)
        for info in disk_info:
            fname = os.path.basename(info['path'])
            if fname == 'disk':
                disk.extend(info['path'],
                            instance['root_gb'] * 1024 * 1024 * 1024)
            elif fname == 'disk.local':
                disk.extend(info['path'],
                            instance['ephemeral_gb'] * 1024 * 1024 * 1024)
            if FLAGS.use_cow_images:
                # back to qcow2 (no backing_file though) so that snapshot
                # will be available
                path_qcow = info['path'] + '_qcow'
                utils.execute('qemu-img', 'convert', '-f', 'raw',
                              '-O', 'qcow2', info['path'], path_qcow)
                utils.execute('mv', path_qcow, info['path'])

        xml = self.to_xml(instance, network_info)
        # assume _create_image do nothing if a target file exists.
        # TODO(oda): injecting files is not necessary
        self._create_image(context, instance, xml,
                                    network_info=network_info,
                                    block_device_info=None)
        self._create_domain_and_network(xml, instance, network_info)
        timer = utils.LoopingCall(self._wait_for_running, instance)
        return timer.start(interval=0.5).wait()

    @exception.wrap_exception()
    def finish_revert_migration(self, instance, network_info):
        LOG.debug(_("Starting finish_revert_migration"),
                   instance=instance)

        inst_base = "%s/%s" % (FLAGS.instances_path, instance['name'])
        inst_base_resize = inst_base + "_resize"
        utils.execute('mv', inst_base_resize, inst_base)

        xml_path = os.path.join(inst_base, 'libvirt.xml')
        xml = open(xml_path).read()
        self._create_domain_and_network(xml, instance, network_info)

        timer = utils.LoopingCall(self._wait_for_running, instance)
        return timer.start(interval=0.5).wait()

    def confirm_migration(self, migration, instance, network_info):
        """Confirms a resize, destroying the source VM"""
        self._cleanup_resize(instance)

    def get_diagnostics(self, instance):
        def get_io_devices(xml_doc):
            """ get the list of io devices from the
            xml document."""
            result = {"volumes": [], "ifaces": []}
            try:
                doc = etree.fromstring(xml_doc)
            except Exception:
                return result
            blocks = [('./devices/disk', 'volumes'),
                ('./devices/interface', 'ifaces')]
            for block, key in blocks:
                section = doc.findall(block)
                for node in section:
                    for child in node.getchildren():
                        if child.tag == 'target' and child.get('dev'):
                            result[key].append(child.get('dev'))
            return result

        domain = self._lookup_by_name(instance['name'])
        output = {}
        # get cpu time, might launch an exception if the method
        # is not supported by the underlying hypervisor being
        # used by libvirt
        try:
            cputime = domain.vcpus()[0]
            for i in range(len(cputime)):
                output["cpu" + str(i) + "_time"] = cputime[i][2]
        except libvirt.libvirtError:
            pass
        # get io status
        xml = domain.XMLDesc(0)
        dom_io = get_io_devices(xml)
        for disk in dom_io["volumes"]:
            try:
                # blockStats might launch an exception if the method
                # is not supported by the underlying hypervisor being
                # used by libvirt
                stats = domain.blockStats(disk)
                output[disk + "_read_req"] = stats[0]
                output[disk + "_read"] = stats[1]
                output[disk + "_write_req"] = stats[2]
                output[disk + "_write"] = stats[3]
                output[disk + "_errors"] = stats[4]
            except libvirt.libvirtError:
                pass
        for interface in dom_io["ifaces"]:
            try:
                # interfaceStats might launch an exception if the method
                # is not supported by the underlying hypervisor being
                # used by libvirt
                stats = domain.interfaceStats(interface)
                output[interface + "_rx"] = stats[0]
                output[interface + "_rx_packets"] = stats[1]
                output[interface + "_rx_errors"] = stats[2]
                output[interface + "_rx_drop"] = stats[3]
                output[interface + "_tx"] = stats[4]
                output[interface + "_tx_packets"] = stats[5]
                output[interface + "_tx_errors"] = stats[6]
                output[interface + "_tx_drop"] = stats[7]
            except libvirt.libvirtError:
                pass
        output["memory"] = domain.maxMemory()
        # memoryStats might launch an exception if the method
        # is not supported by the underlying hypervisor being
        # used by libvirt
        try:
            mem = domain.memoryStats()
            for key in mem.keys():
                output["memory-" + key] = mem[key]
        except libvirt.libvirtError:
            pass
        return output

    def add_to_aggregate(self, context, aggregate, host, **kwargs):
        """Add a compute host to an aggregate."""
        #NOTE(jogo) Currently only used for XenAPI-Pool
        pass

    def remove_from_aggregate(self, context, aggregate, host, **kwargs):
        """Remove a compute host from an aggregate."""
        pass

    def undo_aggregate_operation(self, context, op, aggregate_id,
                                  host, set_error=True):
        """only used for Resource Pools"""
        pass


class HostState(object):
    """Manages information about the compute node through libvirt"""
    def __init__(self, read_only):
        super(HostState, self).__init__()
        self.read_only = read_only
        self._stats = {}
        self.connection = None
        self.update_status()

    def get_host_stats(self, refresh=False):
        """Return the current state of the host.

        If 'refresh' is True, run update the stats first."""
        if refresh:
            self.update_status()
        return self._stats

    def update_status(self):
        """Retrieve status info from libvirt."""
        LOG.debug(_("Updating host stats"))
        if self.connection is None:
            self.connection = LibvirtDriver(self.read_only)
        data = {}
        data["vcpus"] = self.connection.get_vcpu_total()
        data["vcpus_used"] = self.connection.get_vcpu_used()
        data["cpu_info"] = jsonutils.loads(self.connection.get_cpu_info())
        data["disk_total"] = self.connection.get_local_gb_total()
        data["disk_used"] = self.connection.get_local_gb_used()
        data["disk_available"] = data["disk_total"] - data["disk_used"]
        data["host_memory_total"] = self.connection.get_memory_mb_total()
        data["host_memory_free"] = (data["host_memory_total"] -
                                    self.connection.get_memory_mb_used())
        data["hypervisor_type"] = self.connection.get_hypervisor_type()
        data["hypervisor_version"] = self.connection.get_hypervisor_version()
        data["hypervisor_hostname"] = self.connection.get_hypervisor_hostname()

        self._stats = data

        return data
