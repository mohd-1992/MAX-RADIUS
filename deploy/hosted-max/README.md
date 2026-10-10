The MAX tenant uses this controller for metadata reads and journal-authorized factory-reset stop/start of its radius_core only. Other tenants retain their read-only proxy.

Deploy factory_fleet_proxy.py to /opt/saas-manager-next/factory_fleet_proxy.py and reset-controller.conf to /etc/systemd/system/max-panel-fleet@max.service.d/reset-controller.conf, then run systemctl daemon-reload and systemctl restart max-panel-fleet@max.service. The existing tenant socket must remain in place. Do not mount an unrestricted Docker socket in the hosted web container.
