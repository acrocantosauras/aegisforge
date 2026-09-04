from aegisforge.domain.models import (
    Organization,
    Request,
    RequestStatus,
    Role,
    ToolPermission,
    User,
)


def test_organization_and_user_contracts_are_valid() -> None:
    organization = Organization(id="org-001", name="Contoso")
    user = User(
        id="user-001",
        organization_id=organization.id,
        email="alice@contoso.com",
        role=Role.ADMIN,
    )

    assert organization.id == "org-001"
    assert user.organization_id == organization.id
    assert user.role == Role.ADMIN
    assert user.email == "alice@contoso.com"


def test_request_tracks_status_and_permissions() -> None:
    request = Request(
        id="req-001",
        organization_id="org-001",
        requested_by="user-001",
        intent="Summarize customer support escalations",
        status=RequestStatus.PENDING,
        permissions=[
            ToolPermission(name="knowledge.search", allowed=True),
            ToolPermission(name="code.read", allowed=False),
        ],
    )

    assert request.status == RequestStatus.PENDING
    assert request.permissions[0].name == "knowledge.search"
    assert request.permissions[0].allowed is True
    assert request.permissions[1].allowed is False
