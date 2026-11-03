"""Rule registry. Dialect profiles list rule ids in priority order."""

from __future__ import annotations

from ..base import Rule
from .ax import NamespaceFlatten
from .codesys import (ChainedAssignment, EmptyStructInitializer, EndFunctionBlockKeyword, ExportLibraryBlock,
                      HeaderSemicolon)
from .common import (ArrayInitializerBrackets, ElseIfKeyword, KeywordIdentifierRename, PouAccessModifier,
                     StandardNameCollisionRename, VarBlockConstant)
from .scl import (BeginSeparator, BlockAttributes, BoolArrayView, CaseElseColon, ConstInline, DataBlock,
                  LocalPrefix, OrganizationBlock, QuotedIdentifier, Region, ShiftLiteralDword,
                  StandardFormalArguments, TimeLiteralUnderscore, TypeColon, TypedLiteralPrefix)
from .namespaces import LibraryQualifier
from .storage import VarInstMember, VarStatGlobal
from .sysmac import EnEnoCall
from .structure import MemberReorder, PropertyAccessors, VarSectionReorder

RULES: dict[str, Rule] = {
    rule.spec.id: rule
    for rule in (
        VarBlockConstant(),
        ArrayInitializerBrackets(),
        KeywordIdentifierRename(),
        StandardNameCollisionRename(),
        PouAccessModifier(),
        MemberReorder(),
        PropertyAccessors(),
        ExportLibraryBlock(),
        BlockAttributes(),
        QuotedIdentifier(),
        LocalPrefix(),
        TypeColon(),
        BeginSeparator(),
        DataBlock(),
        OrganizationBlock(),
        ConstInline(),
        StandardFormalArguments(),
        TypedLiteralPrefix(),
        Region(),
        TimeLiteralUnderscore(),
        LibraryQualifier(),
        NamespaceFlatten(),
        VarSectionReorder(),
        VarStatGlobal(),
        VarInstMember(),
        ElseIfKeyword(),
        EndFunctionBlockKeyword(),
        ChainedAssignment(),
        HeaderSemicolon(),
        EmptyStructInitializer(),
        BoolArrayView(),
        ShiftLiteralDword(),
        CaseElseColon(),
        EnEnoCall(),
    )
}


def rules_for(profile: dict) -> list[Rule]:
    """The profile's rules in its priority order; unknown ids are an error."""
    result = []
    for rule_id in profile["rules"]:
        rule = RULES[rule_id]
        if profile["family"] not in rule.spec.dialects:
            raise ValueError(f"rule {rule_id} does not support dialect family {profile['family']}")
        result.append(rule)
    return result
