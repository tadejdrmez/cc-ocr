import pytest

from ccocr.admin.mounts import NotShared, configured, to_container, to_host

M = configured(r"C:\cc-ocr\data=/data;c:/OCR_Work_SDS/=/mnt/sds;/srv/scan=/mnt/scan")


def test_windows_paths_map_case_insensitive():
    assert to_container(r"C:\OCR_Work_SDS\data\in", M) == "/mnt/sds/data/in"
    assert to_container(r"c:\ocr_work_sds", M) == "/mnt/sds"
    assert to_container("C:\\cc-ocr\\data\\in\\", M) == "/data/in"
    assert to_container("C:/cc-ocr/data/out", M) == "/data/out"


def test_container_and_linux_host_paths():
    assert to_container("/data/in", M) == "/data/in"
    assert to_container("/srv/scan/in", M) == "/mnt/scan/in"


def test_unshared_folder_explains_how_to_fix():
    with pytest.raises(NotShared, match=r"not shared with the container.*CCOCR_HOST_MOUNTS"):
        to_container(r"D:\Other\in", M)


def test_to_host():
    assert to_host("/mnt/sds/data/in", M) == r"C:\OCR_Work_SDS\data\in"
    assert to_host("/data", M) == r"C:\cc-ocr\data"
    assert to_host("/elsewhere", M) is None
