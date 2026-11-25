"""Knowledge about each supported platform type.

AWX, Ascender and Ansible Automation Platform share the controller REST API but
differ in where it is mounted and in which operator custom resource describes
the installation.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CustomResource:
    group: str
    version: str
    plural: str
    kind: str


@dataclass(frozen=True)
class Platform:
    name: str
    display: str
    # Controller API roots to try, in order. The first one whose ping answers wins.
    api_prefixes: tuple[str, ...]
    # Operator custom resources that may describe the installation, in order of preference.
    custom_resources: tuple[CustomResource, ...]
    # Extra unauthenticated ping endpoints worth recording (label, absolute path).
    extra_pings: tuple[tuple[str, str], ...] = ()


AWX_CR = CustomResource("awx.ansible.com", "v1beta1", "awxs", "AWX")

PLATFORMS: dict[str, Platform] = {
    "awx": Platform(
        name="awx",
        display="AWX",
        api_prefixes=("/api/v2/",),
        custom_resources=(AWX_CR,),
    ),
    "ascender": Platform(
        name="ascender",
        display="Ascender",
        api_prefixes=("/api/v2/",),
        # The Ascender operator deploys the AWX kind; newer releases add an Ascender kind.
        custom_resources=(
            CustomResource("ascender.ansible.com", "v1beta1", "ascenders", "Ascender"),
            AWX_CR,
        ),
    ),
    "aap": Platform(
        name="aap",
        display="Ansible Automation Platform",
        # AAP 2.5+ mounts the controller behind the platform gateway; 2.4 uses /api/v2/.
        api_prefixes=("/api/controller/v2/", "/api/v2/"),
        custom_resources=(
            CustomResource("aap.ansible.com", "v1alpha1", "ansibleautomationplatforms",
                           "AnsibleAutomationPlatform"),
            CustomResource("automationcontroller.ansible.com", "v1beta1", "automationcontrollers",
                           "AutomationController"),
        ),
        extra_pings=(("gateway", "/api/gateway/v1/ping/"),),
    ),
}


def get_platform(name: str) -> Platform:
    try:
        return PLATFORMS[name]
    except KeyError as exc:
        raise ValueError(f"unknown platform type {name!r}; choose from {', '.join(PLATFORMS)}") from exc
