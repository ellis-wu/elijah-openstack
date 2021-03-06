# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
# Copyright 2012 Nebula, Inc.
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

import json
import logging

from django.utils.translation import ugettext_lazy as _

from horizon import exceptions
from horizon import forms
from horizon import workflows
from horizon.utils import validators

from openstack_dashboard import api
from openstack_dashboard.api import glance
from openstack_dashboard.usage import quotas

from ..util import CLOUDLET_TYPE
from ..util import find_basevm_by_sha256
from ..util import find_matching_flavor
from ..util import get_resource_size

import requests
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError

from .. import cloudlet_api
from xml.etree import ElementTree
import urllib2
try:
    from elijah.provisioning import msgpack
except ImportError as e:
    import msgpack
from elijah.provisioning.configuration import Const as Cloudlet_Const


LOG = logging.getLogger(__name__)



class SelectProjectUserAction(workflows.Action):
    project_id = forms.ChoiceField(label=_("Project"))
    user_id = forms.ChoiceField(label=_("User"))

    def __init__(self, request, *args, **kwargs):
        super(SelectProjectUserAction, self).__init__(request, *args, **kwargs)
        # Set our project choices
        projects = [(tenant.id, tenant.name)
                    for tenant in request.user.authorized_tenants]
        self.fields['project_id'].choices = projects

        # Set our user options
        users = [(request.user.id, request.user.username)]
        self.fields['user_id'].choices = users

    class Meta:
        name = _("Project & User")
        # Unusable permission so this is always hidden. However, we
        # keep this step in the workflow for validation/verification purposes.
        permissions = ("!",)


class SelectProjectUser(workflows.Step):
    action_class = SelectProjectUserAction
    contributes = ("project_id", "user_id")


KEYPAIR_IMPORT_URL = "horizon:project:access_and_security:keypairs:import"


class SetResumeDetailAction(workflows.Action):
    image_id = forms.ChoiceField(label=_("Image"), required=True)
    name = forms.CharField(max_length=80, label=_("Instance Name"),
                           initial="resumed_vm")
    security_group_ids = forms.MultipleChoiceField(label=_("Security Groups"),
                                       required=True,
                                       initial=["default"],
                                       widget=forms.CheckboxSelectMultiple(),
                                       help_text=_("Launch instance in these "
                                                   "security groups."))

    flavor = forms.ChoiceField(label=_("Flavor"), required=True,
                              help_text=_("Size of image to launch."))
    #keypair_id = forms.DynamicChoiceField(label=_("Keypair"),
    #                                   required=False,
    #                                   help_text=_("Which keypair to use for "
    #                                               "authentication."),
    #                                   add_item_link=KEYPAIR_IMPORT_URL)

    class Meta:
        name = _("Base VM Info")
        help_text_template = ("project/cloudlet/instance/"
                              "_resume_details_help.html")

    def clean(self):
        cleaned_data = super(SetResumeDetailAction, self).clean()
        return cleaned_data

    def _get_available_images(self, request, context):
        project_id = context.get('project_id', None)
        if not hasattr(self, "_public_images"):
            public = {"is_public": True,
                      "status": "active"}
            try:
                image_detail = api.glance.image_list_detailed(
                    request, filters=public
                )
                if len(image_detail) == 2:  # icehouse
                    public_images, _more = image_detail
                elif len(image_detail) == 3: # kilo
                    public_images, _more , has_prev_data = image_detail
            except:
                public_images = []
                exceptions.handle(request,
                                  _("Unable to retrieve public images."))
            self._public_images = public_images

        # Preempt if we don't have a project_id yet.
        if project_id is None:
            setattr(self, "_images_for_%s" % project_id, [])

        if not hasattr(self, "_images_for_%s" % project_id):
            owner = {"property-owner_id": project_id,
                     "status": "active"}
            try:
                image_detail = api.glance.image_list_detailed(
                    request, filters=owner
                )
                if len(image_detail) == 2:  # icehouse
                    owned_images, _more = image_detail
                elif len(image_detail) == 3: # kilo
                    owned_images, _more , has_prev_data = image_detail
            except:
                owned_images = []
                exceptions.handle(request,
                                  _("Unable to retrieve images for "
                                    "the current project."))
            setattr(self, "_images_for_%s" % project_id, owned_images)

        owned_images = getattr(self, "_images_for_%s" % project_id)
        images = owned_images + self._public_images
        base_vms = list()
        for image in images:
            if hasattr(image, 'properties') == True:
                properties = getattr(image, 'properties')
                cloudlet_type = properties.get('cloudlet_type', None)
                if cloudlet_type == CLOUDLET_TYPE.IMAGE_TYPE_BASE_DISK:
                    base_vms.append(image)

        # Remove duplicate images
        image_ids = []
        final_images = []
        for image in base_vms:
            if image.id not in image_ids:
                image_ids.append(image.id)
                final_images.append(image)
        return [image for image in final_images
                if image.container_format not in ('aki', 'ari')]

    def populate_image_id_choices(self, request, context):
        images = self._get_available_images(request, context)
        choices = [(image.id, image.name)
                   for image in images
                   if image.properties.get("image_type", '') == "snapshot"]
        if choices:
            choices.insert(0, ("", _("Select Base VM")))
        else:
            choices.insert(0, ("", _("No Base VM is available.")))
        return choices

    def get_help_text(self):
        extra = {}
        try:
            extra['usages'] = quotas.tenant_quota_usages(self.request)
            extra['usages_json'] = json.dumps(extra['usages'])
            flavors = json.dumps([f._info for f in
                                       api.nova.flavor_list(self.request)])
            extra['flavors'] = flavors
        except:
            exceptions.handle(self.request,
                              _("Unable to retrieve quota information."))
        return super(SetResumeDetailAction, self).get_help_text(extra)

    def populate_keypair_id_choices(self, request, context):
        try:
            keypairs = api.nova.keypair_list(request)
            keypair_list = [(kp.name, kp.name) for kp in keypairs]
        except:
            keypair_list = []
            exceptions.handle(request,
                              _('Unable to retrieve keypairs.'))
        if keypair_list:
            if len(keypair_list) == 1:
                self.fields['keypair_id'].initial = keypair_list[0][0]
            #keypair_list.insert(0, ("", _("Select a keypair")))
        else:
            keypair_list = (("", _("No keypairs available.")),)
        return keypair_list

    def populate_security_group_ids_choices(self, request, context):
        try:
            groups = api.network.security_group_list(request)
            #groups = api.nova.SecurityGroupManager.list(request)
            security_group_list = [(sg.name, sg.name) for sg in groups]
        except:
            exceptions.handle(request,
                              _('Unable to retrieve list of security groups'))
            security_group_list = []
        return security_group_list

    def populate_flavor_choices(self, request, context):
        # return all flavors of Base VM image 
        try:
            matching_flavors = set()
            flavors = api.nova.flavor_list(request)
            basevm_images = self._get_available_images(request, context)
            for basevm_image in basevm_images:
                if basevm_image.properties is None or\
                        len(basevm_image.properties) == 0:
                    continue
                libvirt_xml_str = basevm_image.properties.get(
                    'base_resource_xml_str', None)
                if libvirt_xml_str is None:
                    continue
                cpu_count, memory_mb = get_resource_size(libvirt_xml_str)
                disk_gb = basevm_image.min_disk
                ret_flavors = find_matching_flavor(flavors,
                                                   cpu_count,
                                                   memory_mb,
                                                   disk_gb)
                matching_flavors.update(ret_flavors)
            if len(matching_flavors) > 0:
                self.fields['flavor'].initial = list(matching_flavors)[0]
            else:
                self.fields['flavor'].initial = (0, "No valid flavor")
        except Exception as e:
            matching_flavors= set()
            exceptions.handle(request,
                              _('Unable to retrieve instance flavors.'))
        return sorted(list(matching_flavors))


class SetSynthesizeDetailsAction(workflows.Action):
    overlay_url = forms.CharField(max_length=200, required=True,
                                  label=_("URL for VM overlay"),
                                  initial="http://")
    name = forms.CharField(max_length=80, label=_("Instance Name"),
                           initial="synthesized_vm")
    security_group_ids = forms.MultipleChoiceField(
        label=_("Security Groups"),
        required=True,
        initial=["default"],
        widget=forms.CheckboxSelectMultiple(),
        help_text=_("Launch instance in these "
                    "security groups."))
    flavor = forms.ChoiceField(label=_("Flavor"), required=True,
                               help_text=_("Size of image to launch."))
    #keypair_id = forms.DynamicChoiceField(label=_("Keypair"),
    #                                   required=False,
    #                                   help_text=_("Which keypair to use for "
    #                                               "authentication."),
    #                                   add_item_link=KEYPAIR_IMPORT_URL)

    class Meta:
        name = _("VM overlay Info")
        help_text_template = ("project/cloudlet/instance/"
                              "_synthesis_details_help.html")

    def clean(self):
        cleaned_data = super(SetSynthesizeDetailsAction, self).clean()

        overlay_url = cleaned_data.get('overlay_url', None)
        if overlay_url is None:
            raise forms.ValidationError(_("Need URL to fetch VM overlay"))

        # check url format
        val = URLValidator()
        try:
            val(overlay_url)
        except ValidationError, e:
            raise forms.ValidationError(_("Malformed URL for VM overlay"))

        # check url accessibility
        try:
            header_ret = requests.head(overlay_url)
            if header_ret.ok == False:
                raise
        except Exception as e:
            msg = "URL is not accessible : %s" % overlay_url
            raise forms.ValidationError(_(msg))

        if cleaned_data.get('name', None) is None:
            raise forms.ValidationError(_("Need name for the synthesized VM"))

        # finally check the header file of VM overlay
        # to make sure that associated Base VM exists
        from elijah.provisioning.package import VMOverlayPackage
        matching_image = None
        requested_basevm_sha256 = ''
        try:
            overlay_package = VMOverlayPackage(overlay_url)
            metadata = overlay_package.read_meta()
            overlay_meta = msgpack.unpackb(metadata)
            requested_basevm_sha256 = overlay_meta.get(Cloudlet_Const.META_BASE_VM_SHA256, None)
            matching_image = find_basevm_by_sha256(self.request, requested_basevm_sha256)
        except Exception as e:
            msg = "Error while finding matching Base VM with %s" % (requested_basevm_sha256)
            raise forms.ValidationError(_(msg))

        if matching_image == None:
            msg = "Cannot find matching base VM with UUID(%s)" % (requested_basevm_sha256)
            raise forms.ValidationError(_(msg))
        else:
            # specify associated base VM from the metadata
            cleaned_data['image_id'] = str(matching_image.id)
            return cleaned_data

    def get_help_text(self):
        extra = {}
        try:
            extra['usages'] = quotas.tenant_quota_usages(self.request)
            extra['usages_json'] = json.dumps(extra['usages'])
            flavors = json.dumps([f._info for f in
                                       api.nova.flavor_list(self.request)])
            extra['flavors'] = flavors
        except:
            exceptions.handle(self.request,
                              _("Unable to retrieve quota information."))
        return super(SetSynthesizeDetailsAction, self).get_help_text(extra)

    def populate_keypair_id_choices(self, request, context):
        try:
            keypairs = api.nova.keypair_list(request)
            keypair_list = [(kp.name, kp.name) for kp in keypairs]
        except:
            keypair_list = []
            exceptions.handle(request,
                              _('Unable to retrieve keypairs.'))
        if keypair_list:
            if len(keypair_list) == 1:
                self.fields['keypair_id'].initial = keypair_list[0][0]
            #keypair_list.insert(0, ("", _("Select a keypair")))
        else:
            keypair_list = (("", _("No keypairs available.")),)
        return keypair_list

    def populate_security_group_ids_choices(self, request, context):
        try:
            groups = api.network.security_group_list(request)
            #groups = api.nova.SecurityGroupManager.list(request)
            security_group_list = [(sg.name, sg.name) for sg in groups]
        except:
            exceptions.handle(request,
                              _('Unable to retrieve list of security groups'))
            security_group_list = []
        return security_group_list

    def _get_available_images(self, request, context):
        project_id = context.get('project_id', None)
        public_images = []
        owned_images = []
        public = {"is_public": True,
                    "status": "active"}
        try:
            image_detail = api.glance.image_list_detailed(
                request, filters=public
            )
            if len(image_detail) == 2:  # icehouse
                public_images, _more = image_detail
            elif len(image_detail) == 3: # kilo
                public_images, _more , has_prev_data = image_detail
        except:
            public_images = []
            pass

        # Preempt if we don't have a project_id yet.
        if project_id is None:
            setattr(self, "_images_for_%s" % project_id, [])

        if not hasattr(self, "_images_for_%s" % project_id):
            owner = {"property-owner_id": project_id,
                     "status": "active"}
            try:
                image_detail = api.glance.image_list_detailed(
                    request, filters=owner
                )
                if len(image_detail) == 2:  # icehouse
                    owned_images, _more = image_detail
                elif len(image_detail) == 3: # kilo
                    owned_images, _more , has_prev_data = image_detail
            except:
                owned_images = []
                pass

        images = owned_images + public_images
        base_vms = list()
        for image in images:
            if hasattr(image, 'properties') == True:
                properties = getattr(image, 'properties')
                cloudlet_type = properties.get('cloudlet_type', None)
                if cloudlet_type == CLOUDLET_TYPE.IMAGE_TYPE_BASE_DISK:
                    base_vms.append(image)

        # Remove duplicate images
        image_ids = []
        final_images = []
        for image in base_vms:
            if image.id not in image_ids:
                image_ids.append(image.id)
                final_images.append(image)
        return [image for image in final_images
                if image.container_format not in ('aki', 'ari')]


    def populate_flavor_choices(self, request, context):
        # return all flavors of Base VM image 
        try:
            matching_flavors = set()
            flavors = api.nova.flavor_list(request)
            basevm_images = self._get_available_images(request, context)
            for basevm_image in basevm_images:
                if basevm_image.properties is None or\
                        len(basevm_image.properties) == 0:
                    continue
                libvirt_xml_str = basevm_image.properties.get(
                    'base_resource_xml_str', None)
                if libvirt_xml_str is None:
                    continue
                cpu_count, memory_mb = get_resource_size(libvirt_xml_str)
                disk_gb = basevm_image.min_disk
                ret_flavors = find_matching_flavor(flavors,
                                                   cpu_count,
                                                   memory_mb,
                                                   disk_gb)
                matching_flavors.update(ret_flavors)
            if len(matching_flavors) > 0:
                self.fields['flavor'].initial = list(matching_flavors)[0]
        except:
            matching_flavors= set()
            exceptions.handle(request,
                              _('Unable to retrieve instance flavors.'))
        return sorted(list(matching_flavors))


class SetResumeAction(workflows.Step):
    action_class = SetResumeDetailAction
    contributes = ("image_id", "name", "security_group_ids", "flavor", "keypair_id")

    def prepare_action_context(self, request, context):
        source_type = request.GET.get("source_type", None)
        source_id = request.GET.get("source_id", None)
        if source_type != None and source_id != None:
            context[source_type] = source_id
        return context


class SetSynthesizeAction(workflows.Step):
    action_class = SetSynthesizeDetailsAction
    contributes = ("image_id", "overlay_url", "name", "security_group_ids", "flavor", "keypair_id")


class SetAccessControlsAction(workflows.Action):
    keypair = forms.DynamicChoiceField(label=_("Keypair"),
                                       required=False,
                                       help_text=_("Which keypair to use for "
                                                   "authentication."),
                                       add_item_link=KEYPAIR_IMPORT_URL)
    groups = forms.MultipleChoiceField(label=_("Security Groups"),
                                       required=True,
                                       initial=["default"],
                                       widget=forms.CheckboxSelectMultiple(),
                                       help_text=_("Launch instance in these "
                                                   "security groups."))

    class Meta:
        name = _("Access & Security")
        help_text = _("Control access to your instance via keypairs, "
                      "security groups, and other mechanisms.")

    def populate_keypair_choices(self, request, context):
        try:
            keypairs = api.nova.keypair_list(request)
            keypair_list = [(kp.name, kp.name) for kp in keypairs]
        except:
            keypair_list = []
            exceptions.handle(request,
                              _('Unable to retrieve keypairs.'))
        if keypair_list:
            if len(keypair_list) == 1:
                self.fields['keypair'].initial = keypair_list[0][0]
            #keypair_list.insert(0, ("", _("Select a keypair")))
        else:
            keypair_list = (("", _("No keypairs available.")),)
        return keypair_list

    def populate_groups_choices(self, request, context):
        try:
            groups = api.network.security_group_list(request)
            #groups = api.nova.SecurityGroupManager.list(request)
            security_group_list = [(sg.name, sg.name) for sg in groups]
        except:
            exceptions.handle(request,
                              _('Unable to retrieve list of security groups'))
            security_group_list = []
        return security_group_list



class ResumeInstance(workflows.Workflow):
    slug = "cloudlet resume base instance"
    name = _("Cloudlet Resume Base VM")
    finalize_button_name = _("Launch")
    success_message = _('Cloudlet launched %(count)s named "%(name)s".')
    failure_message = _('Cloudlet is unable to launch %(count)s named "%(name)s".')
    success_url = "horizon:project:cloudlet:index"
    default_steps = (SelectProjectUser,
                     SetResumeAction,
                     )

    def format_status_message(self, message):
        name = self.context.get('name', 'unknown instance')
        count = self.context.get('count', 1)
        if int(count) > 1:
            return message % {"count": _("%s instances") % count,
                              "name": name}
        else:
            return message % {"count": _("instance"), "name": name}

    def handle(self, request, context):
        dev_mapping = None
        user_script = None
        try:
            api.nova.server_create(request,
                                   context['name'],
                                   context['image_id'],
                                   context['flavor'],
                                   context['keypair_id'],
                                   user_script,
                                   context['security_group_ids'],
                                   dev_mapping,
                                   nics=None,
                                   instance_count=1,
                                   )
            return True
        except:
            exceptions.handle(request)
            return False


class SynthesisInstance(workflows.Workflow):
    slug = "cloudlet syntehsize VM"
    name = _("Cloudlet Synthesize VM")
    finalize_button_name = _("Synthesize")
    success_message = _('Cloudlet synthesized %(count)s named "%(name)s".')
    failure_message = _('Cloudlet is unable to synthesize %(count)s named "%(name)s".')
    success_url = "horizon:project:cloudlet:index"
    default_steps = (SelectProjectUser,
                     SetSynthesizeAction,)

    def format_status_message(self, message):
        name = self.context.get('name', 'unknown instance')
        count = self.context.get('count', 1)
        if int(count) > 1:
            return message % {"count": _("%s instances") % count,
                              "name": name}
        else:
            return message % {"count": _("instance"), "name": name}

    def handle(self, request, context):
        try:
            ret_json = cloudlet_api.request_synthesis(
                    request,
                    context['name'],
                    context['image_id'],
                    context['flavor'],
                    context['keypair_id'],
                    context['security_group_ids'],
                    context['overlay_url'],
                    )
            error_msg = ret_json.get("badRequest", None)
            if error_msg is not None:
                msg = error_msg.get("message", "Failed to request VM synthesis")
                raise Exception(msg)
            return True
        except:
            exceptions.handle(request)
            return False
