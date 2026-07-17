from pydantic import BaseModel, Field


class SubscriptionRequest(BaseModel):
    account_name: str
    subscription_date: str
    subscription_amount: float


class DividendRequest(BaseModel):
    account_name: str
    dividend_date: str
    dividend_amount: float


class InterestDeductionRequest(BaseModel):
    account_name: str
    deduction_date: str
    deduction_amount: float


class WithdrawalRequest(BaseModel):
    account_name: str
    withdrawal_date: str
    withdrawal_amount: float


class FundChangeInput(BaseModel):
    event_timestamp: str
    subscription_amount: float = Field(default=0, ge=0)
    dividend_amount: float = Field(default=0, ge=0)
    interest_deduction: float = Field(default=0, ge=0)
    withdrawal_amount: float = Field(default=0, ge=0)


class FundChangesRequest(BaseModel):
    changes: list[FundChangeInput]
