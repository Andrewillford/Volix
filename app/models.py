"""
Modelos Pydantic da cotação de frete (padrão Intelipost).

Sobrou apenas o que o motor `rate_calculator.quote_for_carrier` produz
(DeliveryMethod/CarrierInfo), reutilizado por auditoria, comparativo do cadastro
e romaneio, além do ErrorResponse padrão da API.
"""
from __future__ import annotations
from typing import List, Optional, Dict
from datetime import date
from pydantic import BaseModel, Field


class CarrierInfo(BaseModel):
    name: str


class DeliveryMethod(BaseModel):
    logistics_provider_name: str
    delivery_method_name: str           # origem da transportadora
    final_shipping_cost: float
    delivery_estimate_business_days: int
    delivery_estimate_date: Optional[date] = None
    carrier: Optional[CarrierInfo] = None
    charges: Optional[Dict[str, float]] = Field(
        None,
        description="Detalhamento dos encargos: frete_peso, gris, pedagio, tas, despacho, tec, ad_valorem, frete_val, sec_cat"
    )


class ErrorResponse(BaseModel):
    status: str = "ERROR"
    message: str
    errors: Optional[List[str]] = None
