# Copyright (c) 2014 Red Hat, Inc.
# All Rights Reserved.
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

import functools

from keystoneauth1 import exceptions as ks_exc
from keystoneauth1 import loading as keystone
from keystoneauth1 import session
from oslo_log import log as logging

import nova.conf
from nova.i18n import _LE, _LI, _LW
from nova import objects

CONF = nova.conf.CONF
LOG = logging.getLogger(__name__)


def safe_connect(f):
    @functools.wraps(f)
    def wrapper(self, *a, **k):
        try:
            # We've failed in a non recoverable way, fully give up.
            if self._disabled:
                return
            return f(self, *a, **k)
        except ks_exc.EndpointNotFound:
            msg = _LW("The placement API endpoint not found. Optional use of "
                      "placement API for reporting is now disabled.")
            LOG.warning(msg)
            self._disabled = True
        except ks_exc.MissingAuthPlugin:
            msg = _LW("No authentication information found for placement API. "
                      "Optional use of placement API for reporting is now "
                      "disabled.")
            LOG.warning(msg)
            self._disabled = True
        except ks_exc.ConnectFailure:
            msg = _LW('Placement API service is not responding.')
            LOG.warning(msg)
    return wrapper


class SchedulerReportClient(object):
    """Client class for updating the scheduler."""

    ks_filter = {'service_type': 'placement',
                 'region_name': CONF.placement.os_region_name}

    def __init__(self):
        # A dict, keyed by the resource provider UUID, of ResourceProvider
        # objects that will have their inventories and allocations tracked by
        # the placement API for the compute host
        self._resource_providers = {}
        auth_plugin = keystone.load_auth_from_conf_options(
            CONF, 'placement')
        self._client = session.Session(auth=auth_plugin)
        # TODO(sdague): use this to disable fully when we don't find
        # the endpoint.
        self._disabled = False

    def get(self, url):
        return self._client.get(
            url,
            endpoint_filter=self.ks_filter, raise_exc=False)

    def post(self, url, data):
        # NOTE(sdague): using json= instead of data= sets the
        # media type to application/json for us. Placement API is
        # more sensitive to this than other APIs in the OpenStack
        # ecosystem.
        return self._client.post(
            url, json=data,
            endpoint_filter=self.ks_filter, raise_exc=False)

    @safe_connect
    def _get_resource_provider(self, uuid):
        """Queries the placement API for a resource provider record with the
        supplied UUID.

        Returns an `objects.ResourceProvider` object if found or None if no
        such resource provider could be found.

        :param uuid: UUID identifier for the resource provider to look up
        """
        resp = self.get("/resource_providers/%s" % uuid)
        if resp.status_code == 200:
            data = resp.json()
            return objects.ResourceProvider(
                    uuid=uuid,
                    name=data['name'],
                    generation=data['generation'],
            )
        elif resp.status_code == 404:
            return None
        else:
            msg = _LE("Failed to retrieve resource provider record from "
                      "placement API for UUID %(uuid)s. "
                      "Got %(status_code)d: %(err_text)s.")
            args = {
                'uuid': uuid,
                'status_code': resp.status_code,
                'err_text': resp.text,
            }
            LOG.error(msg, args)

    @safe_connect
    def _create_resource_provider(self, uuid, name):
        """Calls the placement API to create a new resource provider record.

        Returns an `objects.ResourceProvider` object representing the
        newly-created resource provider object.

        :param uuid: UUID of the new resource provider
        :param name: Name of the resource provider
        """
        url = "/resource_providers"
        payload = {
            'uuid': uuid,
            'name': name,
        }
        resp = self.post(url, payload)
        if resp.status_code == 201:
            msg = _LI("Created resource provider record via placement API "
                      "for resource provider with UUID {0} and name {1}.")
            msg = msg.format(uuid, name)
            LOG.info(msg)
            return objects.ResourceProvider(
                    uuid=uuid,
                    name=name,
                    generation=1,
            )
        elif resp.status_code == 409:
            # Another thread concurrently created a resource provider with the
            # same UUID. Log a warning and then just return the resource
            # provider object from _get_resource_provider()
            msg = _LI("Another thread already created a resource provider "
                      "with the UUID {0}. Grabbing that record from "
                      "the placement API.")
            msg = msg.format(uuid)
            LOG.info(msg)
            return self._get_resource_provider(uuid)
        else:
            msg = _LE("Failed to create resource provider record in "
                      "placement API for UUID %(uuid)s. "
                      "Got %(status_code)d: %(err_text)s.")
            args = {
                'uuid': uuid,
                'status_code': resp.status_code,
                'err_text': resp.text,
            }
            LOG.error(msg, args)

    def _ensure_resource_provider(self, uuid, name=None):
        """Ensures that the placement API has a record of a resource provider
        with the supplied UUID. If not, creates the resource provider record in
        the placement API for the supplied UUID, optionally passing in a name
        for the resource provider.

        The found or created resource provider object is returned from this
        method. If the resource provider object for the supplied uuid was not
        found and the resource provider record could not be created in the
        placement API, we return None.

        :param uuid: UUID identifier for the resource provider to ensure exists
        :param name: Optional name for the resource provider if the record
                     does not exist. If empty, the name is set to the UUID
                     value
        """
        if uuid in self._resource_providers:
            return self._resource_providers[uuid]

        rp = self._get_resource_provider(uuid)
        if rp is None:
            name = name or uuid
            rp = self._create_resource_provider(uuid, name)
            if rp is None:
                return
        self._resource_providers[uuid] = rp
        return rp

    def update_resource_stats(self, compute_node):
        """Creates or updates stats for the supplied compute node.

        :param compute_node: updated nova.objects.ComputeNode to report
        """
        compute_node.save()
        self._ensure_resource_provider(compute_node.uuid,
                                       compute_node.hypervisor_hostname)
