from enum import StrEnum


class FieldRole(StrEnum):
    OPCODE    = "opcode"
    CONSTANT  = "constant"
    RESERVED  = "reserved"
    REGISTER  = "register"
    IMMEDIATE = "immediate"


class AccessMode(StrEnum):
    RW = "rw"
    RO = "ro"
    WO = "wo"
