from functools import partial
from functools import wraps
from http import HTTPStatus
from typing import Tuple
from uuid import UUID

from app_common_python import LoadedConfig
from flask import abort
from flask import g
from flask import request
from requests import Session
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

from api.metrics import outbound_http_response_time
from app import IDENTITY_HEADER
from app import RbacPermission
from app import RbacResourceType
from app import REQUEST_ID_HEADER
from app.auth import get_current_identity
from app.auth.identity import Identity
from app.auth.identity import IdentityType
from app.common import inventory_config
from app.instrumentation import rbac_failure
from app.instrumentation import rbac_group_permission_denied
from app.instrumentation import rbac_permission_denied
from app.logging import get_logger


logger = get_logger(__name__)

RBAC_ROUTE = "/api/rbac/v1/access/?application="
CHECKED_TYPES = [IdentityType.USER, IdentityType.SERVICE_ACCOUNT]
RETRY_STATUSES = [500, 502, 503, 504]

outbound_http_metric = outbound_http_response_time.labels("rbac")


def get_rbac_url(app: str) -> str:
    return inventory_config().rbac_endpoint + RBAC_ROUTE + app


def tenant_translator_url() -> str:
    return inventory_config().tenant_translator_url


def get_rbac_permissions(app: str, request_header: dict):
    request_session = Session()
    retry_config = Retry(total=inventory_config().rbac_retries, backoff_factor=1, status_forcelist=RETRY_STATUSES)
    request_session.mount(get_rbac_url(app), HTTPAdapter(max_retries=retry_config))

    try:
        with outbound_http_metric.time():
            rbac_response = request_session.get(
                url=get_rbac_url(app),
                headers=request_header,
                timeout=inventory_config().rbac_timeout,
                verify=LoadedConfig.tlsCAPath,
            )
    except Exception as e:
        rbac_failure(logger, e)
        abort(503, "Failed to reach RBAC endpoint, request cannot be fulfilled")
    finally:
        request_session.close()

    resp_data = rbac_response.json()
    logger.debug("Fetched RBAC Data", extra=resp_data)

    return resp_data["data"]


# Determine whether the request should be allowed, strictly according to the given permissions.
# If any of these match, the endpoint should at least be allowed (but may have filtered results).
def _is_request_allowed_by_permission(
    rbac_permission: str, permission_base: str, resource_type: RbacResourceType, required_permission: RbacPermission
) -> bool:
    return (
        rbac_permission  # inventory:*:*
        == f"{permission_base}:{RbacResourceType.ALL.value}:{RbacPermission.ADMIN.value}"
        or rbac_permission  # inventory:{type}:*
        == f"{permission_base}:{resource_type.value}:{RbacPermission.ADMIN.value}"
        or rbac_permission  # inventory:*:(read | write)
        == f"{permission_base}:{RbacResourceType.ALL.value}:{required_permission.value}"
        or rbac_permission  # inventory:{type}:(read | write)
        == f"{permission_base}:{resource_type.value}:{required_permission.value}"
    )


# Validate RBAC response, and fetch
def _get_group_list_from_resource_definition(resource_definition: dict) -> list[str]:
    if "attributeFilter" in resource_definition:
        if resource_definition["attributeFilter"].get("key") != "group.id":
            abort(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Invalid value for attributeFilter.key in RBAC response.",
            )
        elif resource_definition["attributeFilter"].get("operation") != "in":
            abort(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Invalid value for attributeFilter.operation in RBAC response.",
            )
        elif not isinstance(resource_definition["attributeFilter"]["value"], list):
            abort(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Did not receive a list for attributeFilter.value in RBAC response.",
            )
        else:
            # Validate that all values in the filter are UUIDs.
            group_list = resource_definition["attributeFilter"]["value"]
            try:
                for gid in group_list:
                    if gid is not None:
                        UUID(gid)
            except ValueError:
                abort(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "Received invalid UUIDs for attributeFilter.value in RBAC response.",
                )

            return group_list


def get_rbac_filter(
    resource_type: RbacResourceType,
    required_permission: RbacPermission,
    identity: Identity,
    rbac_request_headers: dict,
    permission_base: str = "inventory",
) -> Tuple[str, dict]:
    # Returns a 2-item tuple:
    # 1. Whether or not the request should be allowed at all
    # 2. The filter that should be applied to the data (if allowed)
    if inventory_config().bypass_rbac:
        return True, None

    if identity.identity_type not in CHECKED_TYPES:
        if resource_type == RbacResourceType.HOSTS:
            return True, None
        else:
            return False, None

    # track that RBAC is being used to control access
    g.access_control_rule = "RBAC"
    logger.debug("access_control_rule set")

    rbac_data = get_rbac_permissions(permission_base, rbac_request_headers)
    allowed = False  # Determines whether the endpoint can be accessed at all
    allowed_group_ids = set()  # If populated, limits the allowed resources to specific group IDs

    for rbac_permission in rbac_data:
        if _is_request_allowed_by_permission(
            rbac_permission["permission"], permission_base, resource_type, required_permission
        ):
            allowed = True

            # Get the list of allowed Group IDs from the attribute filter.
            groups_attribute_filter = set()
            for resourceDefinition in rbac_permission["resourceDefinitions"]:
                group_list = _get_group_list_from_resource_definition(resourceDefinition)
                groups_attribute_filter.update(group_list)

            if groups_attribute_filter:
                # If the RBAC permission is applicable and is limited to specific group IDs,
                # add that list of group IDs to the list of allowed group IDs.
                allowed_group_ids.update(groups_attribute_filter)
            else:
                # If any applicable RBAC permission exists and is NOT limited to specific group IDs,
                # call the usual endpoint without resource-specific access limitations.
                return True, None

    # If all applicable permissions are restricted to specific groups,
    # call the endpoint with the RBAC filtering data.
    if allowed:
        return True, {"groups": allowed_group_ids}
    else:
        rbac_permission_denied(logger, required_permission.value, rbac_data)
        return False, None


def rbac(resource_type: RbacResourceType, required_permission: RbacPermission, permission_base: str = "inventory"):
    def other_func(func):
        @wraps(func)
        def modified_func(*args, **kwargs):
            if inventory_config().bypass_rbac:
                return func(*args, **kwargs)

            current_identity = get_current_identity()

            request_headers = {
                IDENTITY_HEADER: request.headers[IDENTITY_HEADER],
                REQUEST_ID_HEADER: request.headers.get(REQUEST_ID_HEADER),
            }

            allowed, rbac_filter = get_rbac_filter(
                resource_type, required_permission, current_identity, request_headers, permission_base
            )
            if allowed:
                if rbac_filter:
                    return partial(func, rbac_filter=rbac_filter)(*args, **kwargs)
                else:
                    return func(*args, **kwargs)
            else:
                abort(HTTPStatus.FORBIDDEN)

        return modified_func

    return other_func


def rbac_group_id_check(rbac_filter: dict, requested_ids: set) -> None:
    if rbac_filter and "groups" in rbac_filter:
        # Find the IDs that are in requested_ids but not rbac_filter
        disallowed_ids = requested_ids.difference(rbac_filter["groups"])
        if len(disallowed_ids) > 0:
            # id check is only called before writing to groups, permission so far is always the same
            required_permission = "inventory:groups:write"
            joined_ids = ", ".join(disallowed_ids)
            rbac_group_permission_denied(logger, joined_ids, required_permission)
            abort(HTTPStatus.FORBIDDEN, f"You do not have access to the the following groups: {joined_ids}")
