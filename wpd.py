"""Fast phone enumeration through the Windows Portable Devices API (about 5x faster than the Shell)."""
import ctypes
from ctypes import POINTER, c_ulong, c_wchar_p

import comtypes
import comtypes.client

comtypes.client.GetModule("portabledeviceapi.dll")
comtypes.client.GetModule("portabledevicetypes.dll")
import comtypes.gen.PortableDeviceApiLib as port  # noqa: E402
import comtypes.gen.PortableDeviceTypesLib as types  # noqa: E402

INPROC = comtypes.CLSCTX_INPROC_SERVER
_FMT = comtypes.GUID("{EF6B490D-5CD8-437A-AFFC-DA8B60EE4A3C}")
_FOLDERISH = {comtypes.GUID("{27E2E392-A111-48E0-AB0C-E17705A05F85}"),   # folder
              comtypes.GUID("{99ED0160-17FF-4C44-9D98-1D7A6F941921}")}   # functional object (storage)
_ole32 = ctypes.windll.ole32


def _key(pid):
    k = port._tagpropertykey()
    k.fmtid = _FMT
    k.pid = pid
    return k


K_NAME, K_TYPE, K_SIZE, K_ORIG = _key(4), _key(7), _key(11), _key(12)


class Device:
    def __init__(self, pnp_id):
        comtypes.CoInitialize()
        info = comtypes.client.CreateObject(types.PortableDeviceValues, clsctx=INPROC, interface=port.IPortableDeviceValues)
        self.dev = comtypes.client.CreateObject(port.PortableDevice, clsctx=INPROC, interface=port.IPortableDevice)
        self.dev.Open(pnp_id, info)
        self.content = self.dev.Content()
        self.props = self.content.Properties()
        self.keys = comtypes.client.CreateObject(types.PortableDeviceKeyCollection, clsctx=INPROC,
                                                 interface=port.IPortableDeviceKeyCollection)
        for k in (K_NAME, K_TYPE, K_SIZE, K_ORIG):
            self.keys.Add(k)

    def children(self, obj_id):
        en = self.content.EnumObjects(0, obj_id, None)
        out = []
        while True:
            arr = (ctypes.c_void_p * 512)()
            got = c_ulong(0)
            en._IEnumPortableDeviceObjectIDs__com_Next(512, ctypes.cast(arr, POINTER(c_wchar_p)), ctypes.byref(got))
            if not got.value:
                return out
            for i in range(got.value):
                out.append(ctypes.wstring_at(arr[i]))
                _ole32.CoTaskMemFree(ctypes.c_void_p(arr[i]))

    def info(self, obj_id):
        """(name, is_folder, size)"""
        v = self.props.GetValues(obj_id, self.keys)
        try:
            name = v.GetStringValue(K_ORIG)
        except comtypes.COMError:
            name = v.GetStringValue(K_NAME)
        try:
            folder = v.GetGuidValue(K_TYPE) in _FOLDERISH
        except comtypes.COMError:
            folder = False
        size = 0
        if not folder:
            try:
                size = v.GetUnsignedLargeIntegerValue(K_SIZE)
            except comtypes.COMError:
                pass
        return name, folder, size

    def find(self, names):
        """Object id for a path of names below the device root."""
        cur = "DEVICE"
        for name in names:
            cur = next((c for c in self.children(cur) if self.info(c)[0] == name), None)
            if cur is None:
                raise FileNotFoundError(name)
        return cur

    def close(self):
        try:
            self.dev.Close()
        except comtypes.COMError:
            pass


def pnp_id_from_shell_path(path):
    """The Shell path of a phone is '::{This PC GUID}\\<PnP device id>'."""
    return path[path.index("\\") + 1:].split("\\SID-")[0]
