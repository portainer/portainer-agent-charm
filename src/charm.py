#!/usr/bin/env python3
# Copyright 2021 Portainer
# See LICENSE file for licensing details.

import logging
import sys
import utils

from kubernetes import kubernetes
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

# disable bytecode caching according to: https://discourse.charmhub.io/t/upgrading-a-charm/1131
sys.dont_write_bytecode = True
logger = logging.getLogger(__name__)
# Reduce the log output from the Kubernetes library
# logging.getLogger("kubernetes").setLevel(logging.INFO)
CHARM_VERSION = 1.0
SERVICETYPE_LB = "LoadBalancer"
SERVICETYPE_CIP = "ClusterIP"
SERVICETYPE_NP = "NodePort"
CONFIG_SERVICETYPE = "service_type"
CONFIG_SERVICEHTTPPORT = "service_http_port"
CONFIG_SERVICEHTTPNODEPORT = "service_http_node_port"
CONFIG_EDGE = "edge"
CONFIG_EDGE_ID = "edge_id"
CONFIG_EDGE_KEY = "edge_key"

class PortainerAgentCharm(CharmBase):
    """Charm the service."""
    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        logger.info(f"initialising charm, version: {CHARM_VERSION}", )
        # setup default config value, only create if not exist
        self._stored.set_default(
            charm_version = CHARM_VERSION,
            config = self._default_config,
        )
        logger.debug(f"start with config: {self._config}")
        # hooks up events
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.start, self._start_portainer_agent)
        self.framework.observe(self.on.portainer_agent_pebble_ready, self._start_portainer_agent)
        self.framework.observe(self.on.upgrade_charm, self._upgrade_charm)

    def _on_install(self, event):
        """Handle the install event, create Kubernetes resources"""
        logger.info("installing charm")
        if not self._k8s_auth():
            self.unit.status = WaitingStatus('waiting for k8s auth')
            logger.info("waiting for k8s auth, installation deferred")
            event.defer()
            return
        self.unit.status = MaintenanceStatus("creating kubernetes service for portainer agent")
        logger.info("creating kubernetes services for portainer agent")
        headless_name = f"{self.app.name}-headless"
        self._create_k8s_service(headless_name, self._build_k8s_headless_service(headless_name))
        self._create_k8s_service(self.app.name, self._build_k8s_service_by_config(self.app.name, self._config))

    def _create_k8s_service(self, name: str, body: kubernetes.client.V1Service):
        """Delete then create k8s service by name and body."""
        logger.info("creating k8s service")
        api = kubernetes.client.CoreV1Api(kubernetes.client.ApiClient())
        try:
            api.delete_namespaced_service(name = name, namespace = self.namespace)
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                logger.info("%s service doesn't exist, skip deletion", name)
            else:
                raise e
        api.create_namespaced_service(
            namespace = self.namespace,
            body = body,
        )

    def _build_k8s_headless_service(self, name: str) -> kubernetes.client.V1Service:
        """Constructs k8s agent headless service spec by input config"""
        return kubernetes.client.V1Service(
            api_version = "v1",
            metadata = kubernetes.client.V1ObjectMeta(
                namespace = self.namespace,
                name = name,
            ),
            spec = kubernetes.client.V1ServiceSpec(
                    cluster_ip = "None",
                    selector = {
                        "app.kubernetes.io/name": self.app.name,
                    }
                )
        )

    def _build_k8s_service_by_config(self, name: str, config: dict) -> kubernetes.client.V1Service:
        """Constructs k8s agent service spec by input config"""
        return kubernetes.client.V1Service(
            api_version = "v1",
            metadata = kubernetes.client.V1ObjectMeta(
                namespace = self.namespace,
                name = name,
            ),
            spec = self._build_k8s_spec_by_config(config),
        )

    def _build_k8s_spec_by_config(self, config: dict) -> kubernetes.client.V1ServiceSpec:
        """Constructs k8s service spec by input config"""
        service_type = config[CONFIG_SERVICETYPE]
        http_port = kubernetes.client.V1ServicePort(
            name = "http",
            port = config[CONFIG_SERVICEHTTPPORT],
            target_port = 9001,
        )
        if (service_type == SERVICETYPE_NP 
            and CONFIG_SERVICEHTTPNODEPORT in config):
            http_port.node_port = config[CONFIG_SERVICEHTTPNODEPORT]

        result = kubernetes.client.V1ServiceSpec(
            type = service_type,
            ports = [
                http_port
            ],
            selector = {
                "app.kubernetes.io/name": self.app.name,
            },
        )

        if config[CONFIG_EDGE]:
            result.cluster_ip = "None"
            result.type = SERVICETYPE_CIP

        logger.debug(f"generating spec: {result}")
        return result

    def _on_config_changed(self, event):
        """Handles the configuration changes"""
        logger.info("configuring charm")
        # self.model.config is the aggregated config in the current runtime
        logger.debug(f"current config: {self._config} vs future config: {self.model.config}")
        if not self._validate_config(self.model.config):
            self.unit.status = WaitingStatus('waiting for a valid config')
            logger.info("waiting for a valid config, configuration deferred")
            event.defer()
            return
        # merge the runtime config with stored one
        new_config = { **self._config, **self.model.config }
        if self._has_config_change(new_config, [CONFIG_SERVICEHTTPNODEPORT, CONFIG_SERVICETYPE, CONFIG_EDGE]):
            if not self._k8s_auth():
                self.unit.status = WaitingStatus('waiting for k8s auth')
                logger.info("waiting for k8s auth, configuration deferred")
                event.defer()
                return
            self._patch_k8s_service_by_config(self.app.name, new_config)
        # update pebble if edge config is changed
        if self._has_config_change(new_config, [CONFIG_EDGE, CONFIG_EDGE_ID, CONFIG_EDGE_KEY, CONFIG_SERVICEHTTPPORT]):
            self._update_pebble(event, new_config)
        # set the config
        self._config = new_config
        logger.debug(f"merged config: {self._config}")

    def _upgrade_charm(self, _):
        """Handle charm upgrade"""
        logger.info(f"upgrading from {self._stored.charm_version} to {CHARM_VERSION}")
        if CHARM_VERSION < self._stored.charm_version:
            logger.error("downgrade is not supported")
        elif CHARM_VERSION == self._stored.charm_version:
            logger.info("nothing to upgrade")
        else:
            # upgrade logic here
            logger.info("nothing to upgrade")

    # Issue with this method:
    # _build_k8s_spec_by_config generates the object that leaves 'None'
    # when stringified, e.g. {'cluster_ip': None,'external_i_ps': None,'external_name': None},
    # causing k8s complaining: Invalid value: []core.IPFamily(nil): primary ipFamily can not be unset
    def _patch_k8s_service_by_config(self, name: str, new_config: dict):
        """Patch k8s service by stored config."""
        logger.info("updating k8s service by config")
        client = kubernetes.client.ApiClient()
        api = kubernetes.client.CoreV1Api(client)
        # a direct replacement of /spec won't work, since it misses things like cluster_ip;
        # need to serialize the object to dictionay then clean none entries to replace bits by bits.
        spec = utils.clean_nones(
            client.sanitize_for_serialization(
                self._build_k8s_spec_by_config(new_config)))
        body = []
        for k, v in spec.items():
            body.append({
                "op": "replace",
                "path": f"/spec/{k}",
                "value": v,
            })
        logger.debug(f"patching with body: {body}")
        if body:
            api.patch_namespaced_service(
                name = name,
                namespace = self.namespace,
                body = body,
            )
        else:
            logger.info("nothing to patch, skip patching")
            return

    def _update_pebble(self, event, config: dict):
        """Update pebble by config"""
        logger.info("updating pebble")
        # get a reference to the portainer workload container
        agent_name = self.app.name
        container = self.unit.get_container(agent_name)
        if container.can_connect():
            svc = container.get_services().get(agent_name, None)
            # check if the pebble service is already running
            if svc:
                logger.info("stopping pebble service")
                container.stop(agent_name)
            # override existing layer
            container.add_layer(agent_name, self._build_layer_by_config(event, config), combine = True)
            logger.info("starting pebble service")
            container.start(agent_name)
        else:
            self.unit.status = WaitingStatus('waiting for container to start')
            logger.info("waiting for container to start, update pebble deferred")
            event.defer()

    def _build_layer_by_config(self, event, config: dict) -> dict:
        """Returns a pebble layer by config"""
        self._k8s_auth()
        api = kubernetes.client.CoreV1Api(kubernetes.client.ApiClient())
        pod_ip = None
        agent_cluster_ip = None
        logger.info(f"{self.unit.name} -> {self.unit.name.replace('/', '-')}")
        # gets the pod ip
        try:
            pod = api.read_namespaced_pod(self._pod_name, self.namespace)
            pod_ip = pod.status.pod_ip
            logger.debug(f"Portainer Agent Pod IP: {pod_ip}")
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                logger.error(f"pod {self._pod_name} doesn't exist yet")
                self.unit.status = WaitingStatus('waiting for pod to start')
                # we still allow the agent to be started
                # the pod ip would be setup next time pebble needs refresh
            else:
                raise e
        # gets the service cluster ip
        try:
            service = api.read_namespaced_service(self.app.name, self.namespace)
            agent_cluster_ip = service.spec.cluster_ip
            logger.debug(f"Portainer Agent Service Cluster IP: {agent_cluster_ip}")
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                logger.error(f"service {self.app.name} doesn't exist yet")
                self.unit.status = WaitingStatus('waiting for service to start')
                # we still allow the agent to be started
                # the cluster ip would be setup next time pebble needs refresh
            else:
                raise e
        return {
            "services": {
                self.app.name: {
                    "override": "replace",
                    "command": "./agent",
                    "startup": "enabled",
                    "environment": {
                        "AGENT_PORT": config[CONFIG_SERVICEHTTPPORT],
                        "AGENT_CLUSTER_ADDR": agent_cluster_ip,
                        "KUBERNETES_POD_IP": pod_ip,
                        "EDGE": "1" if config[CONFIG_EDGE] else "0",
                        "EDGE_ID": config[CONFIG_EDGE_ID],
                        "EDGE_KEY": config[CONFIG_EDGE_KEY],
                    },
                }
            },
        }

    def _start_portainer_agent(self, event):
        """Function to handle starting Portainer using Pebble"""
        # Get a reference to the portainer workload container
        agent_name = "portainer-agent"
        container = self.unit.get_container(agent_name)
        if container.can_connect():
            svc = container.get_services().get(agent_name, None)
            # Check if the service is already running
            if not svc:
                # Add a new layer
                container.add_layer(agent_name, self._build_layer_by_config(event, self._config), combine = True)
                container.start(agent_name)

            self.unit.status = ActiveStatus()

    def _has_config_change(self, target: dict, keys: list) -> bool:
        """Compares values of the keys in the current and target config, return True if any of the values is different"""
        for k in keys:
            if self._config.get(k) != target.get(k):
                return True
        return False

    def _validate_config(self, config: dict) -> bool:
        """Validates the input config"""
        if not config.get(CONFIG_EDGE):
            if config.get(CONFIG_SERVICETYPE) not in (SERVICETYPE_CIP, SERVICETYPE_LB, SERVICETYPE_NP):
                logger.error(f"agent config - service type {config.get(CONFIG_SERVICETYPE)} is not recognized")
                return False
            if config.get(CONFIG_SERVICEHTTPPORT) is None:
                logger.error(f"agent config - service http or edge port cannot be None")
                return False
        else:
            if config.get(CONFIG_EDGE_ID) is None or config.get(CONFIG_EDGE_KEY) is None:
                logger.error(f"edge config - edge_id and edge_key cannot be None")
                return False
        return True

    @property
    def _pod_name(self) -> str:
        return self.unit.name.replace("/", "-")

    @property
    def _config(self) -> dict:
        """Returns the stored config"""
        return self._stored.config

    @_config.setter
    def _config(self, config: dict):
        """Sets the stored config to input"""
        self._stored.config = config

    @property
    def _default_config(self) -> dict:
      """Returns the default config of this charm, which sets:

      - service.type to LoadBalancer
      - service.httpPort to 9001
      - service.httpNodePort to 30778
      - edge_enabled to False
      - edge_id to empty string
      - edge_key to empty string
      """
      return {
          CONFIG_SERVICETYPE: SERVICETYPE_LB,
          CONFIG_SERVICEHTTPPORT: 9001,
          CONFIG_SERVICEHTTPNODEPORT: 30778,
          CONFIG_EDGE: False,
          CONFIG_EDGE_ID: "",
          CONFIG_EDGE_KEY: "",
      }

    def _k8s_auth(self) -> bool:
        """Authenticate to kubernetes."""
        # Authenticate against the Kubernetes API using a mounted ServiceAccount token
        kubernetes.config.load_incluster_config()
        # Test the service account we've got for sufficient perms
        api = kubernetes.client.CoreV1Api(kubernetes.client.ApiClient())

        try:
            api.list_namespaced_service(namespace = self.namespace)
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 403:
                # If we can't read a cluster role, we don't have enough permissions
                self.unit.status = BlockedStatus("Run juju trust on this application to continue")
                return False
            else:
                raise e
        return True

    @property
    def namespace(self) -> str:
        """Fetch the current Kubernetes namespace by reading it from the service account"""
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r") as f:
            return f.read().strip()

if __name__ == "__main__":
    main(PortainerAgentCharm, use_juju_for_storage = True)
