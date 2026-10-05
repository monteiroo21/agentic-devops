# How to run the dummy server

```
sudo nano /etc/hosts
```

After that, add the following lines at the end of the file:

```
127.0.0.1  www.veloribeira.pt www.douroride.pt www.trailgear-supply.com routes.pedalmap.eu www.portobikefest.pt
127.0.0.2  www.ciclovia-norte.pt
```

Run:

```
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80
```

So the server can use port 80 without root, ensuring that no :8080 shows up in the URLs.

Finally, run the server:

```
uv run server.py
```
