import uuid


def test_add_host_to_group(
    db_create_group, db_create_host, db_get_hosts_for_group, db_create_host_group_assoc, api_add_hosts_to_group
):
    # Create a group and 3 hosts
    group_id = db_create_group("test_group").id
    host_id_list = [db_create_host().id for _ in range(3)]

    response_status, _ = api_add_hosts_to_group(group_id, [str(host) for host in host_id_list[0:2]])
    assert response_status == 200

    # Confirm that the group now only contains  2 hosts
    hosts_after = db_get_hosts_for_group(group_id)
    assert len(hosts_after) == 2
    assert hosts_after[0].id == host_id_list[0]
    assert hosts_after[1].id == host_id_list[1]


def test_add_associated_host_to_same_group(
    db_create_group, db_create_host, db_get_hosts_for_group, db_create_host_group_assoc, api_add_hosts_to_group
):
    # Create a group and 3 hosts
    group_id = db_create_group("test_group").id
    host_id_list = [db_create_host().id for _ in range(3)]

    # Add all 3 hosts to the group
    for host_id in host_id_list:
        db_create_host_group_assoc(host_id, group_id)

    # Confirm that the association exists
    hosts_before = db_get_hosts_for_group(group_id)
    assert len(hosts_before) == 3

    # Confirm that the API is  allowed to add the hosts even one is associated
    response_status, _ = api_add_hosts_to_group(group_id, [str(host) for host in host_id_list[0:2]])
    assert response_status == 200


def test_add_associated_host_to_different_group(
    db_create_group, db_create_host, db_get_hosts_for_group, db_create_host_group_assoc, api_add_hosts_to_group
):
    # Create a group and 3 hosts
    group1_id = db_create_group("test_group").id
    group2_id = db_create_group("test_group2").id
    host_id_list = [db_create_host().id for _ in range(3)]

    # Add all 3 hosts to the group
    db_create_host_group_assoc(host_id_list[0], group1_id)
    db_create_host_group_assoc(host_id_list[1], group1_id)
    db_create_host_group_assoc(host_id_list[2], group2_id)

    # Confirm that the association exists
    hosts_before = db_get_hosts_for_group(group1_id)
    assert len(hosts_before) == 2
    hosts_before = db_get_hosts_for_group(group2_id)
    assert len(hosts_before) == 1

    # Confirm that the API is  allowed to add the hosts even one is associated
    response_status, _ = api_add_hosts_to_group(group1_id, [str(host_id_list[2])])
    assert response_status == 400


def test_add_host_list_with_one_associated_host_to_group(
    db_create_group, db_create_host, db_get_hosts_for_group, db_create_host_group_assoc, api_add_hosts_to_group
):
    # Create a group and 3 hosts
    group_id = db_create_group("test_group").id
    host_id_list = [db_create_host().id for _ in range(3)]

    # Add first and last hosts to the group
    db_create_host_group_assoc(host_id_list[0], group_id)
    db_create_host_group_assoc(host_id_list[2], group_id)

    # adding only id[1], since 0 and 2 already associated above
    response_status, _ = api_add_hosts_to_group(group_id, [str(host) for host in host_id_list])
    assert response_status == 200

    # Confirm that the group now only contains  2 hosts
    hosts_after = db_get_hosts_for_group(group_id)
    assert len(hosts_after) == 3


def test_add_host_to_missing_group(
    db_create_group, db_create_host, db_get_hosts_for_group, db_create_host_group_assoc, api_add_hosts_to_group
):
    # Create a test group which not exist in database
    missing_group_id = "454dddba-9a4d-42b3-8f16-86a8c1400000"
    host_id_list = [db_create_host().id for _ in range(3)]

    response_status, _ = api_add_hosts_to_group(missing_group_id, [str(host) for host in host_id_list])
    assert response_status == 404


def test_add_missing_host_to_existing_group(
    db_create_group, db_create_host, db_get_hosts_for_group, db_create_host_group_assoc, api_add_hosts_to_group
):
    group_id = db_create_group("test_group").id
    host_id_list = [str(uuid.uuid4())]

    response_status, _ = api_add_hosts_to_group(group_id, host_id_list)
    assert response_status == 400


def test_with_empty_data(
    db_create_group, db_create_host, db_get_hosts_for_group, db_create_host_group_assoc, api_add_hosts_to_group
):
    response_status, _ = api_add_hosts_to_group(None, None)
    assert response_status == 400