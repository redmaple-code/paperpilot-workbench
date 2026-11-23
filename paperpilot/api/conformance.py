from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from paperpilot.services import requirement_conformance as conformance
from paperpilot.services import product_form as forms


class JudgementIn(BaseModel):
    target: str = Field(..., min_length=3, description="REQ-001::AC-001-01 或 REQ-001::*")
    verdict: str = Field(..., description="correct / wrong / partial / unverified")
    cause: str = "unknown"
    expected: str = ""
    observed: str = ""
    note: str = ""
    evidence: str = ""


class ScaffoldIn(BaseModel):
    requirement_id: str = Field(..., min_length=3)
    force: bool = False


class FormConfirmIn(BaseModel):
    form_id: str = Field(..., min_length=3)
    source_refs: list[str] = Field(..., min_length=1)
    expected_sha256: str = Field(..., min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    note: str = ""


def create_router(project_root: Callable[[], Path]) -> APIRouter:
    router = APIRouter(prefix="/api/conformance", tags=["requirement conformance"])

    @router.get("")
    def matrix() -> dict[str, Any]:
        try:
            return conformance.build_matrix(project_root())
        except FileNotFoundError as exc:
            raise HTTPException(404, detail={"code": "ledger_missing", "message": str(exc)}) from exc

    @router.post("/scan")
    def scan() -> dict[str, Any]:
        try:
            return conformance.scan(project_root())
        except FileNotFoundError as exc:
            raise HTTPException(404, detail={"code": "ledger_missing", "message": str(exc)}) from exc

    @router.post("/judgements")
    def judge(payload: JudgementIn) -> dict[str, Any]:
        try:
            return conformance.save_judgement(
                project_root(),
                target=payload.target,
                verdict=payload.verdict,
                cause=payload.cause,
                expected=payload.expected,
                observed=payload.observed,
                note=payload.note,
                evidence=payload.evidence,
            )
        except ValueError as exc:
            raise HTTPException(400, detail={"code": "invalid_judgement", "message": str(exc)}) from exc

    @router.delete("/judgements")
    def clear(target: str) -> dict[str, Any]:
        return conformance.save_judgement(project_root(), target=target, verdict="unverified")

    @router.get("/report", response_class=PlainTextResponse)
    def report() -> str:
        try:
            matrix_payload = conformance.build_matrix(project_root())
        except FileNotFoundError as exc:
            raise HTTPException(404, detail={"code": "ledger_missing", "message": str(exc)}) from exc
        return conformance.render_report(matrix_payload)

    @router.get("/catalog")
    def catalog() -> dict[str, Any]:
        return {"verdicts": conformance.VERDICTS, "verdict_labels": conformance.VERDICT_LABELS, "root_causes": conformance.ROOT_CAUSES}

    @router.get("/chain")
    def chain() -> dict[str, Any]:
        try:
            return forms.build_chain(project_root())
        except FileNotFoundError as exc:
            raise HTTPException(404, detail={"code": "ledger_missing", "message": str(exc)}) from exc

    @router.post("/chain/scan")
    def scan_chain() -> dict[str, Any]:
        try:
            return forms.write_chain(project_root())
        except FileNotFoundError as exc:
            raise HTTPException(404, detail={"code": "ledger_missing", "message": str(exc)}) from exc

    @router.post("/forms/scaffold")
    def scaffold_form(payload: ScaffoldIn) -> dict[str, Any]:
        try:
            return forms.scaffold(project_root(), payload.requirement_id, force=payload.force)
        except KeyError as exc:
            raise HTTPException(404, detail={"code": "requirement_missing", "message": str(exc)}) from exc
        except FileExistsError as exc:
            raise HTTPException(409, detail={"code": "form_exists", "message": str(exc)}) from exc

    @router.post("/forms/confirm")
    def confirm_form(payload: FormConfirmIn) -> dict[str, Any]:
        try:
            return forms.confirm(project_root(), payload.form_id, payload.source_refs, payload.note, payload.expected_sha256)
        except FileNotFoundError as exc:
            raise HTTPException(404, detail={"code": "form_missing", "message": str(exc)}) from exc
        except ValueError as exc:
            raise HTTPException(400, detail={"code": "form_not_ready", "message": str(exc)}) from exc

    return router
