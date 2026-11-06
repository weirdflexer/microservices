from .models import PaymentResponse
from hmac import compare_digest
from uuid import UUID

from fastapi import APIRouter, Header, Query

from common.errors import DomainError
from .models import PaymentResult, RefundResult


def make_router(service):
    router = APIRouter(prefix="/api", tags=["Платежи: учебный провайдер"])

    def authorize(token):
        expected = getattr(service.ctx.settings, "provider_token", "demo-provider-token")
        if token is None or not compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            raise DomainError(401, "Некорректный X-Provider-Token")

    @router.get("/payments", summary="Список платежей", response_model=list[PaymentResponse])
    def list_payments(booking_id: UUID | None = None, offset: int = Query(0, ge=0),
                      limit: int = Query(100, ge=1, le=100)):
        return service.list(str(booking_id) if booking_id else None, offset, limit)

    @router.get("/payments/{payment_id}", summary="Платёж и статус возврата", response_model=PaymentResponse)
    def get(payment_id: UUID):
        return service.get(str(payment_id))

    @router.post("/payments/{payment_id}:result", summary="Учебный callback результата оплаты", response_model=PaymentResponse)
    def result(payment_id: UUID, body: PaymentResult, x_provider_token: str | None = Header(None)):
        authorize(x_provider_token)
        return service.result(str(payment_id), body.model_dump())

    @router.post("/payments/{payment_id}:refund-result", summary="Учебный callback результата возврата", response_model=PaymentResponse)
    def refund_result(payment_id: UUID, body: RefundResult, x_provider_token: str | None = Header(None)):
        authorize(x_provider_token)
        return service.refund_result(str(payment_id), body.model_dump())

    return router
