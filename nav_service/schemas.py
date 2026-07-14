from pydantic import BaseModel


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
