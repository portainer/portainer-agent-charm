#!/usr/bin/env python3
# Copyright 2021 Portainer
# See LICENSE file for licensing details.

import logging
import utils
from typing import Protocol

from kubernetes import kubernetes
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

logger = logging.getLogger(__name__)
# Reduce the log output from the Kubernetes library
# logging.getLogger("kubernetes").setLevel(logging.INFO)
CHARM_VERSION = 1.0
# PORTAINER_AGENT_IMG = "portainer/agent:2.7.0"
SERVICE_VERSION = "portainer-agent-2.7.0"
SERVICETYPE_LB = "LoadBalancer"
SERVICETYPE_CIP = "ClusterIP"
SERVICETYPE_NP = "NodePort"
CONFIG_SERVICETYPE = "service_type"
CONFIG_SERVICEHTTPPORT = "service_http_port"
CONFIG_SERVICEHTTPNODEPORT = "service_http_node_port"
EDGE = "edge"
EDGE_ID = "edge_id"
EDGE_KEY = "edge_key"
AGENTTYPE_AGENT = "agent"
AGENTTYPE_EDGE = "edge-agent"
AGENTTYPE = AGENTTYPE_AGENT

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

    def _on_install(self, event):
        """Handle the install event, create Kubernetes resources"""
        logger.info("installing charm")
        config = self._config
        if not self._k8s_auth():
            self.unit.status = WaitingStatus('waiting for k8s auth')
            logger.info("waiting for k8s auth, installation deferred")
            event.defer()
            return
        if (config[EDGE] == 1):
            self.unit.status = MaintenanceStatus("creating kubernetes service for portainer edge agent")
            logger.info("creating kubernetes service for portainer edge agent")
        else:
            self.unit.status = MaintenanceStatus("creating kubernetes service for portainer agent")
            logger.info("creating kubernetes service for portainer agent")
        self._create_k8s_headless_service_by_config(self._config)
        self._create_k8s_service_by_config(self._config)

    def _create_k8s_headless_service_by_config(self, config: dict):
        """Delete then create k8s headless service by stored config."""
        agent_headless_name = "portainer-agent-headless"
        logger.info("creating k8s headless service")
        api = kubernetes.client.CoreV1Api(kubernetes.client.ApiClient())
        try:
            api.delete_namespaced_service(name=agent_headless_name, namespace=self.namespace)
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                logger.info("%s service doesn't exist, skip deletion", agent_headless_name)
            else:
                raise e
        api.create_namespaced_service(
            namespace=self.namespace,
            body=self._build_k8s_headless_service_by_config(self._config),
        )

    def _create_k8s_service_by_config(self, config: dict):
        """Delete then create k8s service by stored config."""
        agent_name = "portainer-agent"
        logger.info("creating k8s service")
        api = kubernetes.client.CoreV1Api(kubernetes.client.ApiClient())
        try:
            api.delete_namespaced_service(name=agent_name, namespace=self.namespace)
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                logger.info("%s service doesn't exist, skip deletion", agent_name)
            else:
                raise e
        api.create_namespaced_service(
            namespace=self.namespace,
            body=self._build_k8s_service_by_config(self._config),
        )

    def _build_k8s_headless_service_by_config(self, config: dict) -> kubernetes.client.V1Service:
        """Constructs k8s agent headless service spec by input config"""
        agent_headless_name = "portainer-agent-headless"
        return kubernetes.client.V1Service(
            api_version="v1",
            metadata=kubernetes.client.V1ObjectMeta(
                namespace=self.namespace,
                name=agent_headless_name,
            ),
            spec=kubernetes.client.V1ServiceSpec(
                    cluster_ip="None",
                    selector={
                        "app.kubernetes.io/name": self.app.name,
                    }
                )
        )

    def _build_k8s_service_by_config(self, config: dict) -> kubernetes.client.V1Service:
        """Constructs k8s agent service spec by input config"""
        agent_name = "portainer-agent"
        return kubernetes.client.V1Service(
            api_version="v1",
            metadata=kubernetes.client.V1ObjectMeta(
                namespace=self.namespace,
                name=agent_name,
                labels={
                    "io.portainer.kubernetes.application.stack": self.app.name,
                    "app.kubernetes.io/name": self.app.name,
                    "app.kubernetes.io/instance": self.app.name,
                    "app.kubernetes.io/version": SERVICE_VERSION,
                },
            ),
            spec=self._build_k8s_spec_by_config(config),
        )

    def _build_k8s_spec_by_config(self, config: dict) -> kubernetes.client.V1ServiceSpec:
        """Constructs k8s service spec by input config"""
        service_type = config[CONFIG_SERVICETYPE]
        http_port = kubernetes.client.V1ServicePort(
            name="http",
            port=config[CONFIG_SERVICEHTTPPORT],
            target_port=9001,
        )
        if (service_type == SERVICETYPE_NP 
            and CONFIG_SERVICEHTTPNODEPORT in config):
            http_port.node_port = config[CONFIG_SERVICEHTTPNODEPORT]

        result = kubernetes.client.V1ServiceSpec(
            type=service_type,
            ports=[
                http_port
            ],
            selector={
                "app.kubernetes.io/name": self.app.name,
            },
        )
        logger.debug(f"generating spec: {result}")
        return result

    def _on_config_changed(self, event):
        """Handles the configuration changes"""
        logger.info("configuring charm")
        # self.model.config is the aggregated config in the current runtime
        logger.debug(f"current config: {self._config} vs future config: {self.model.config}")
        # merge the runtime config with stored one
        new_config = { **self._config, **self.model.config }
        if new_config != self._config:
            if not self._k8s_auth():
                self.unit.status = WaitingStatus('waiting for k8s auth')
                logger.info("waiting for k8s auth, configuration deferred")
                event.defer()
                return
            self._patch_k8s_service_by_config(new_config)
        # update pebble if service type is changed to or from nodeport or agent type is changed to or from edge
        if ((new_config[CONFIG_SERVICETYPE] != self._config[CONFIG_SERVICETYPE]
            and (new_config[CONFIG_SERVICETYPE] == SERVICETYPE_NP 
                or self._config[CONFIG_SERVICETYPE] == SERVICETYPE_NP)) or (new_config[AGENTTYPE] != self._config[AGENTTYPE]
            and (new_config[AGENTTYPE] == AGENTTYPE_EDGE 
                or self._config[AGENTTYPE] == AGENTTYPE_EDGE))):
            self._update_pebble(event, new_config)
        # set the config
        self._config = new_config
        logger.debug(f"merged config: {self._config}")

    # Issue with this method:
    # _build_k8s_spec_by_config generates the object that leaves 'None'
    # when stringified, e.g. {'cluster_ip': None,'external_i_ps': None,'external_name': None},
    # causing k8s complaining: Invalid value: []core.IPFamily(nil): primary ipFamily can not be unset
    def _patch_k8s_service_by_config(self, new_config: dict):
        """Patch k8s service by stored config."""
        logger.info("updating k8s service by config")
        client = kubernetes.client.ApiClient()
        api = kubernetes.client.CoreV1Api(client)
        agent_name = "portainer-agent"
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
                name=agent_name,
                namespace=self.namespace,
                body=body,
            )
        else:
            logger.info("nothing to patch, skip patching")
            return

    def _update_pebble(self, event, config: dict):
        """Update pebble by config"""
        logger.info("updating pebble")
        # get a reference to the portainer workload container
        agent_name = "portainer-agent"
        container = self.unit.get_container(agent_name)
        if container.is_ready():
            svc = container.get_services().get(agent_name, None)
            # check if the pebble service is already running
            if svc:
                logger.info("stopping pebble service")
                container.stop(agent_name)
            # override existing layer
            container.add_layer(agent_name, self._build_layer_by_config(config), combine=True)
            logger.info("starting pebble service")
            container.start(agent_name)
        else:
            self.unit.status = WaitingStatus('waiting for container to start')
            logger.info("waiting for container to start, update pebble deferred")
            event.defer()

    def _build_layer_by_config(self, config: dict) -> dict:
        """Returns a pebble layer by config"""
        self._k8s_auth()
        api = kubernetes.client.CoreV1Api(kubernetes.client.ApiClient())
        agent_name = "portainer-agent"
        pod_list = api.list_namespaced_pod("portainer")
        for pod in pod_list.items:
            if agent_name in pod.metadata.name:
                logging.info("Portainer Agent Pod: %s", pod.metadata.name)
                logging.info("Portainer Agent Pod IP: %s", pod.status.pod_ip)
                pod_ip=pod.status.pod_ip
        service_list = api.list_namespaced_service("portainer")
        for service in service_list.items:
            if service.metadata.name == agent_name:
                logging.info("Portainer Agent Service: %s", service.metadata.name)
                logging.info("Portainer Agent Service Cluster IP: %s", service.spec.cluster_ip)
                agent_cluster_ip=service.spec.cluster_ip
        if config[EDGE] == 1:
            command = "./agent"
            return {
                "services": {
                    agent_name: {
                        "override": "replace",
                        "command": command,
                        "startup": "enabled",
                        "environment": {
                            "LOG_LEVEL": "DEBUG",
                            "AGENT_PORT": "9001",
                            "AGENT_CLUSTER_ADDR": agent_cluster_ip,
                            "KUBERNETES_POD_IP": pod_ip,
                            "EDGE": "1",
                            "EDGE_ID": config[EDGE_ID],
                            "EDGE_KEY": config[EDGE_KEY],
                        },
                    }
                },
            }
        else:
            command = "./agent"
            return {
                "services": {
                    agent_name: {
                        "override": "replace",
                        "command": command,
                        "startup": "enabled",
                        "environment": {
                            "LOG_LEVEL": "DEBUG",
                            "AGENT_PORT": "9001",
                            "AGENT_CLUSTER_ADDR": agent_cluster_ip,
                            "KUBERNETES_POD_IP": pod_ip
                        },
                    }
                },
            }

    def _start_portainer_agent(self, _):
        """Function to handle starting Portainer using Pebble"""
        # Get a reference to the portainer workload container
        agent_name = "portainer-agent"
        container = self.unit.get_container(agent_name)
        with container.is_ready():
            svc = container.get_services().get(agent_name, None)
            # Check if the service is already running
            if not svc:
                # Add a new layer
                container.add_layer(agent_name, self._build_layer_by_config(self._config), combine=True)
                container.start(agent_name)

            self.unit.status = ActiveStatus()

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
      - agenttype to agent
      """
      return {
          CONFIG_SERVICETYPE: SERVICETYPE_LB,
          CONFIG_SERVICEHTTPPORT: 9001,
          CONFIG_SERVICEHTTPNODEPORT: 30778,
          EDGE: 0,
          EDGE_ID: "",
          EDGE_KEY: "",
          AGENTTYPE: AGENTTYPE_AGENT,
      }

    def _k8s_auth(self) -> bool:
        """Authenticate to kubernetes."""
        # Authenticate against the Kubernetes API using a mounted ServiceAccount token
        kubernetes.config.load_incluster_config()
        # Test the service account we've got for sufficient perms
        api = kubernetes.client.CoreV1Api(kubernetes.client.ApiClient())

        try:
            api.list_namespaced_service(namespace=self.namespace)
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
    main(PortainerAgentCharm, use_juju_for_storage=True)
