#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
Custom Ansible module: multicred_connect

Attempts to log in to a network device (Cisco IOS/IOS-XE/NX-OS/ASA, etc.)
using a list of candidate credentials and a list of candidate protocols
(ssh, telnet). Stops at the first successful combination. Optionally
runs a command once connected.

This module intentionally does NOT use Ansible's network connection
plugins (network_cli) because those require the credential/protocol to
be known *before* the play starts (set on the host/connection vars).
Here we need to try combinations at runtime, per host, so we drive the
connection directly with Netmiko from inside the module.
"""

from ansible.module_utils.basic import AnsibleModule
import traceback

NETMIKO_IMPORT_ERROR = None
try:
    from netmiko import (
        ConnectHandler,
        NetmikoAuthenticationException,
        NetmikoTimeoutException,
    )
    HAS_NETMIKO = True
except Exception:
    HAS_NETMIKO = False
    NETMIKO_IMPORT_ERROR = traceback.format_exc()

try:
    from paramiko.ssh_exception import SSHException
    HAS_PARAMIKO = True
except Exception:
    HAS_PARAMIKO = False
    SSHException = Exception


# Map our protocol keyword + device_type "family" to the concrete
# Netmiko device_type string. Netmiko uses separate device_type values
# for telnet (suffix _telnet) vs ssh (base name).
DEVICE_TYPE_MAP = {
    ("cisco_ios", "ssh"): "cisco_ios",
    ("cisco_ios", "telnet"): "cisco_ios_telnet",
    ("cisco_xe", "ssh"): "cisco_xe",
    ("cisco_xe", "telnet"): "cisco_ios_telnet",
    ("cisco_nxos", "ssh"): "cisco_nxos",
    ("cisco_nxos", "telnet"): "cisco_nxos_telnet",
    ("cisco_asa", "ssh"): "cisco_asa",
    ("cisco_asa", "telnet"): "cisco_asa_telnet",
    ("cisco_xr", "ssh"): "cisco_xr",
    ("cisco_xr", "telnet"): "cisco_xr_telnet",
}


def build_device_type(base_type, protocol):
    """Resolve the Netmiko device_type for a given base family + protocol."""
    key = (base_type, protocol)
    if key in DEVICE_TYPE_MAP:
        return DEVICE_TYPE_MAP[key]
    # Fallback: generic suffix rule for any other cisco_* / generic types
    if protocol == "telnet":
        if base_type.endswith("_telnet"):
            return base_type
        return "{0}_telnet".format(base_type)
    return base_type


def flatten(text):
    """Collapse a multi-line exception message into a single, CSV/log
    friendly line (Netmiko exceptions often span many lines of
    troubleshooting hints we don't need verbatim in a report)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " | ".join(lines)


def try_connect(host, port, base_device_type, protocol, username, password,
                 secret, timeout, command):
    """
    Attempt a single connection with one credential/protocol combo.

    Returns a dict describing the outcome:
        {
          "ok": bool,
          "error": str or None,
          "output": str or None,
        }
    """
    device_type = build_device_type(base_device_type, protocol)

    device_params = {
        "device_type": device_type,
        "host": host,
        "username": username,
        "password": password,
        "secret": secret if secret else password,
        "timeout": timeout,
        "session_timeout": timeout,
        "auth_timeout": timeout,
        "banner_timeout": timeout,
        "fast_cli": False,
    }

    if port:
        device_params["port"] = port

    conn = None
    try:
        conn = ConnectHandler(**device_params)
        # Some platforms need enable() to run privileged show commands.
        try:
            if not conn.check_enable_mode():
                conn.enable()
        except Exception:
            # Not fatal: some devices/users are already privileged or
            # enable isn't applicable (e.g. read-only views). We still
            # consider the login itself successful.
            pass

        output = None
        if command:
            output = conn.send_command(command)

        return {"ok": True, "error": None, "output": output}

    except NetmikoAuthenticationException as exc:
        return {"ok": False, "error": "auth_failed: " + flatten(str(exc)), "output": None}
    except NetmikoTimeoutException as exc:
        return {"ok": False, "error": "timeout: " + flatten(str(exc)), "output": None}
    except (SSHException,) as exc:
        return {"ok": False, "error": "ssh_error: " + flatten(str(exc)), "output": None}
    except Exception as exc:
        return {"ok": False, "error": "error: " + flatten(str(exc)), "output": None}
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass


def run_module():
    module_args = dict(
        host=dict(type="str", required=True),
        port=dict(type="int", required=False, default=None),
        device_type=dict(type="str", required=False, default="cisco_ios"),
        protocols=dict(type="list", elements="str", required=False, default=["ssh", "telnet"]),
        credentials=dict(
            type="list",
            elements="dict",
            required=True,
            options=dict(
                label=dict(type="str", required=False, default=None),
                username=dict(type="str", required=True),
                password=dict(type="str", required=True, no_log=True),
                secret=dict(type="str", required=False, default=None, no_log=True),
            ),
        ),
        command=dict(type="str", required=False, default=None),
        timeout=dict(type="int", required=False, default=15),
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=False,
    )

    if not HAS_NETMIKO:
        module.fail_json(
            msg="The netmiko python package is required on the Ansible "
                "controller for this module. Install it with: "
                "pip install netmiko",
            error=NETMIKO_IMPORT_ERROR,
        )

    host = module.params["host"]
    port = module.params["port"]
    base_device_type = module.params["device_type"]
    protocols = module.params["protocols"]
    credentials = module.params["credentials"]
    command = module.params["command"]
    timeout = module.params["timeout"]

    if not credentials:
        module.fail_json(msg="credentials list is empty for host {0}".format(host))

    if not protocols:
        protocols = ["ssh"]

    attempts_log = []
    result = {
        "host": host,
        "status": "fail",
        "protocol_used": None,
        "credential_used": None,
        "command_output": None,
        "attempts": 0,
        "last_error": None,
        "changed": False,
    }

    # Outer loop: credentials, in the order supplied (first in list = first tried)
    # Inner loop: protocols, in the order supplied (e.g. try ssh, then telnet)
    for cred in credentials:
        username = cred.get("username")
        password = cred.get("password")
        secret = cred.get("secret")
        cred_label = cred.get("label") or username

        for protocol in protocols:
            result["attempts"] += 1
            outcome = try_connect(
                host=host,
                port=port,
                base_device_type=base_device_type,
                protocol=protocol,
                username=username,
                password=password,
                secret=secret,
                timeout=timeout,
                command=command,
            )

            attempts_log.append({
                "credential": cred_label,
                "protocol": protocol,
                "ok": outcome["ok"],
                "error": outcome["error"],
            })

            if outcome["ok"]:
                result["status"] = "success"
                result["protocol_used"] = protocol
                result["credential_used"] = cred_label
                result["command_output"] = outcome["output"]
                result["last_error"] = None
                module.exit_json(
                    msg="Login succeeded on {0} using credential '{1}' over {2}".format(
                        host, cred_label, protocol
                    ),
                    **result
                )
            else:
                result["last_error"] = outcome["error"]
                # fall through and try next protocol / credential

    # If we get here, every credential/protocol combination failed.
    result["status"] = "fail"
    module.exit_json(
        msg="All {0} credential/protocol combinations failed for host {1}".format(
            result["attempts"], host
        ),
        **result
    )


def main():
    run_module()


if __name__ == "__main__":
    main()
