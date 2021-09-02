#!/usr/bin/env python3
# Copyright 2021 Portainer
# See LICENSE file for licensing details.

import logging
from typing import Protocol

from kubernetes import kubernetes
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus

logger = logging.getLogger(__name__)
# Reduce the log output from the Kubernetes library
# logging.getLogger("kubernetes").setLevel(logging.INFO)


class PortainerAgentCharm(CharmBase):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.start, self._start_portainer_agent)
        self.framework.observe(self.on.portainer_agent_pebble_ready, self._start_portainer_agent)

    def _on_install(self, event):
        """Handle the install event, create Kubernetes resources"""
        if not self._k8s_auth():
            event.defer()
            return
        self.unit.status = MaintenanceStatus("patching kubernetes service for portainer agent")
        api = kubernetes.client.CoreV1Api(kubernetes.client.ApiClient())
        api.delete_namespaced_service(name="portainer-agent", namespace=self.namespace)
        api.create_namespaced_service(**self._service_headless)
        if not self._check_portaineragent_headless():
            logging.info("Waiting for agent headless service...")
            event.defer()
            return
        else:
            logging.info("Agent headless service exists")
        api.create_namespaced_service(**self._service)

    def _start_portainer_agent(self, _):
        """Function to handle starting Portainer Agent using Pebble"""
        # Get a reference to the portainer agent workload container
        container = self.unit.get_container("portainer-agent")
        with container.is_ready():
            svc = container.get_services().get("portainer-agent", None)
            # Check if the service is already running
            if not svc:
                # Add a new layer and start the container
                container.add_layer("portainer-agent", self._layer, combine=True)
                logging.info("Pebble layer added")
                if container.get_service("portainer-agent").is_running():
                    container.stop("portainer-agent")
                container.start("portainer-agent")
                self.unit.status = ActiveStatus()

    def _check_portaineragent_headless(self):
        """Check if the Portainer agent headless service exists"""
        self._k8s_auth()
        api = kubernetes.client.CoreV1Api(kubernetes.client.ApiClient())
        existing = None
        try:
            existing = api.read_namespaced_service(
                name="portainer-agent-headless",
                namespace=self.namespace,
            )
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                logger.info("Portainer agent headless service doesn't exist")
                return False
            else:
                raise e
        if not existing:
            logger.info("Portainer agent headless service doesn't exist")
            return False
        return True

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
    def _layer(self) -> dict:
        """Returns a pebble layer for Portainer Agent"""
        self._k8s_auth()
        api = kubernetes.client.CoreV1Api(kubernetes.client.ApiClient())
        pod_list = api.list_namespaced_pod("portainer")
        for pod in pod_list.items:
            if "portainer-agent" in pod.metadata.name:
                logging.info("Portainer Agent Pod: %s", pod.metadata.name)
                logging.info("Portainer Agent Pod IP: %s", pod.status.pod_ip)
                pod_ip=pod.status.pod_ip
        service_list = api.list_namespaced_service("portainer")
        for service in service_list.items:
            if "portainer-agent" in service.metadata.name and ("headless" not in service.metadata.name or "endpoints" not in service.metadata.name):
                logging.info("Portainer Agent Service: %s", service.metadata.name)
                logging.info("Portainer Agent Service Cluster IP: %s", service.spec.cluster_ip)
                agent_cluster_ip=service.spec.cluster_ip
        pebble_layer = {
            "summary": "portainer-agent layer",
            "description": "Pebble config layer for portainer-agent",
            "services": {
                "portainer-agent": {
                    "override": "replace",
                    "command": "./agent",
                    "startup": "enabled",
                    "environment": {
                        "LOG_LEVEL": "DEBUG",
                        "AGENT_CLUSTER_ADDR": agent_cluster_ip,
                        "KUBERNETES_POD_IP": pod_ip
                    },
                }
            },
        }
        logging.info(pebble_layer)
        return pebble_layer

    @property
    def _service(self) -> dict:
        """Return a Kubernetes service setup for Portainer Agent"""
        return {
            "namespace": self.namespace,
            "body": kubernetes.client.V1Service(
                api_version="v1",
                metadata=kubernetes.client.V1ObjectMeta(
                    namespace=self.namespace,
                    name=self.app.name,
                ),
                spec=kubernetes.client.V1ServiceSpec(
                    type="NodePort",
                    ports=[
                        kubernetes.client.V1ServicePort(
                            name="http",
                            port=9001,
                            target_port=9001,
                            protocol="TCP",
                            node_port=30778,
                        ),
                    ],
                    selector={
                        "app.kubernetes.io/name": self.app.name,
                    },
                ),
            ),
        }

    @property
    def _service_headless(self) -> dict:
        """Return a Kubernetes service setup for Portainer Agent Headless"""
        return {
            "namespace": self.namespace,
            "body": kubernetes.client.V1Service(
                api_version="v1",
                metadata=kubernetes.client.V1ObjectMeta(
                    namespace=self.namespace,
                    name="portainer-agent-headless",
                ),
                spec=kubernetes.client.V1ServiceSpec(
                    cluster_ip="None",
                    selector={
                        "app.kubernetes.io/name": self.app.name,
                    }
                )
            )
        }

    @property
    def namespace(self) -> str:
        """Fetch the current Kubernetes namespace by reading it from the service account"""
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r") as f:
            return f.read().strip()

if __name__ == "__main__":
    main(PortainerAgentCharm, use_juju_for_storage=True)
