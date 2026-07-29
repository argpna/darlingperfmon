"""Jinja filters for the Darling role."""


def darling_server_id(storage_name):
    """Derive a Darling server_id from a storage name.

    Mirrors upstream ServerIdHelper.GetDeterministicHashCode: FNV-1a over UTF-16 code
    units, truncated to a signed 32-bit int. The service derives the same value from the
    same name, so a row inserted here is the row it collects into.
    """
    digest = 2166136261
    for char in str(storage_name):
        digest = ((digest ^ ord(char)) * 16777619) & 0xFFFFFFFF
    return digest - 0x100000000 if digest >= 0x80000000 else digest


def darling_storage_name(host, database=None, read_only_intent=False):
    """Build the name that is hashed into a server_id.

    Mirrors upstream ServerIdHelper.BuildStorageName: the database is appended for Azure
    SQL Database so databases on one logical server differ, and ":RO" for read-only intent.
    """
    name = f"{host}:{database}" if database else str(host)
    return f"{name}:RO" if read_only_intent else name


class FilterModule:  # pylint: disable=too-few-public-methods
    """Expose the Darling id filters to Jinja."""

    def filters(self):
        """Return the filter map."""
        return {
            "darling_server_id": darling_server_id,
            "darling_storage_name": darling_storage_name,
        }
