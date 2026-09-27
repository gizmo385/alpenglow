# Docker proxy

Services that need the Docker API reach it through this proxy
([wollomatic/socket-proxy](https://github.com/wollomatic/socket-proxy)), not
by mounting `/var/run/docker.sock`. The socket is root on the host, even
mounted read-only. The proxy only forwards the API calls a service is allowed
to make.

## Layout

| Path | What |
|---|---|
| `compose.yaml` | The two proxies: `docker-proxy` (for most services) and `docker-proxy-beszel` (a unix socket for beszel-agent). |
| `profiles.yaml` | Named access profiles that services extend. **Edit this** to change what services may do. |

## Giving a service Docker access

1. Pick the smallest profile that covers what it does:

   | Profile | Allows | Used by |
   |---|---|---|
   | `docker-read` | List and inspect containers, images and networks; events; version/info | caddy, cup, updates-tracker |
   | `docker-read-stats` | `docker-read` + live container stats | glances |
   | `docker-read-logs` | `docker-read` + container logs | signoz-alloy |
   | `docker-manage` | Everything, including creating containers (**root-equivalent**) | alpenglow_dashboard |

2. Extend it in the service, and declare the network:

   ```yaml
   services:
     myservice:
       extends:
         file: ../docker-proxy/profiles.yaml
         service: docker-read
       # ...the rest of the service as usual; don't mount docker.sock

   networks:
     docker-proxy:
       external: true
   ```

   This sets `DOCKER_HOST=tcp://docker-proxy:2375`, puts the service on the
   internal `docker-proxy` network, and adds the labels the proxy reads its
   permissions from. Most Docker clients honour `DOCKER_HOST`. Some take the
   address as a setting instead (cup: `-s tcp://docker-proxy:2375`; Alloy:
   `host = "tcp://docker-proxy:2375"`).

3. `sudo docker compose up -d`, use the service, then check for refusals:

   ```sh
   sudo docker logs docker-proxy 2>&1 | grep blocked
   ```

   Each line shows the method, the path and the client IP
   (`sudo docker network inspect docker-proxy` maps IPs to containers).

## Allowing another endpoint

If a service needs something its profile doesn't allow, add one named line to
the profile in `profiles.yaml`:

```yaml
      socket-proxy.allow.get.container-top: '(/v[0-9.]+)?/containers/[^/]+/top'
```

- The label is `socket-proxy.allow.<method>.<name>`. The name is only for
  readability, but it must be unique within the profile.
- The value is a Go regexp matched against the whole request path (the proxy
  adds `^`/`$`; query strings aren't included).
- `(/v[0-9.]+)?` matches the optional API version prefix (`/v1.47/...`).
- The path arrives still URL-encoded: image names look like
  `images/docker.io%2Flibrary%2Fredis:alpine/json`, so match them with `.+`.

If only one service needs the endpoint, add a profile that extends an existing
one (like `docker-read-stats` does) rather than widening it for everyone.
Recreate the affected services afterwards: the proxy reads their labels when
they start.

## Exceptions

- **beszel-agent** runs on the host network, so the proxy can't identify it on
  a Docker network. It gets its own read-only unix socket from
  `docker-proxy-beszel`, and its rules are the `-allowGET=...` flags in
  `compose.yaml`.
- **signoz-obi** keeps the raw socket. It runs privileged with `pid: host`,
  which is already full host access, so filtering its Docker API adds nothing.

## Things to know

- Anything allowed to create containers can create a privileged one, so
  `docker-manage` is root-equivalent. No proxy can tell those apart.
- Read access isn't harmless either: inspecting a container returns its
  environment variables, secrets included.
- The proxy has no authentication; its network is the boundary. Never publish
  its port or put it on a shared network like `caddy`.
- **Restarting the proxy briefly takes every site down.** When caddy-docker-proxy
  can't list containers, it doesn't keep its last config; it loads the base
  Caddyfile alone, which has none of the label-defined sites. They come back
  on Caddy's next refresh after the proxy is up again (tested: about 30s). Alloy
  also stops receiving container logs while the proxy is down. So restart or
  update the proxy deliberately, not casually. The proxy restarts itself if it
  loses the Docker socket.
- Before changing Caddy's access, test with a throwaway `caddy-docker-proxy`
  container on the same profile and diff its
  `/config/caddy/Caddyfile.autosave` against the live one. A Caddy that can't
  read labels generates a config with no sites.
