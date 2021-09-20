# Portainer agent

## Description

Portainer is a lightweight ‘universal’ management GUI that can be used to easily manage Docker, Swarm, Kubernetes and ACI environments. It is designed to be as simple to deploy as it is to use.

Portainer consists of a single container that can run on any cluster. It can be deployed as a Linux container or a Windows native container.

Portainer allows you to manage all your orchestrator resources (containers, images, volumes, networks and more) through a super-simple graphical interface.

This fully supported version of Portainer is available for business use. Visit http://www.portainer.io to learn more.

This Juju charm will help you deploy the Portainer agent allowing you to manage multiple container environments from a single Portainer instance.

## Usage

Create a Juju model for Portainer:

```
juju add-model portainer
```

Deploy the Portainer agent:

```
juju deploy portainer-agent --trust
```

Give the Portainer agent cluster access:

```
juju trust portainer-agent --scope=cluster
```

This will deploy the Portainer agent and expose it over an external load balancer on port 9001.

## Configuration

You can deploy Portainer and expose it over ClusterIP if you prefer:

```
juju config portainer-agent service_type=ClusterIP service_http_port=9001
```

You can also use Node port:

```
juju config portainer-agent service_type=NodePort service_http_port=9001 service_http_node_port=30778
```


## Developing

Create and activate a virtualenv with the development requirements:

    virtualenv -p python3 venv
    source venv/bin/activate
    pip install -r requirements-dev.txt

## Testing

The Python operator framework includes a very nice harness for testing
operator behaviour without full deployment. Just `run_tests`:

    ./run_tests
