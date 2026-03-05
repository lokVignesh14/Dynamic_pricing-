from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional

class AmenityForecastRequest(BaseModel):
    amenity_name: str = Field(..., description="Name of the amenity (e.g., Gym, Pool)")
    forecast_choice: str = Field("Next 1 Week", description="Forecast period: Now, Next 2 Days, Next 1 Week, Next 2 Weeks, Next 3 Weeks")

class AmenityForecastResponse(BaseModel):
    amenity_name: str
    forecast_period: str
    last_data_date: str
    forecast_start_date: str
    actual_data: List[Dict[str, Any]]
    forecast_data: List[Dict[str, Any]]
    ai_analysis: str
    time_suggestion: str

class PersonalizedPackageRequest(BaseModel):
    guest_name: str = Field(..., description="Guest name to search for")

class PersonalizedPackageResponse(BaseModel):
    guest_name: str
    matched_guest_name: str
    analysis: str

class EnergyTrackingRequest(BaseModel):
    amenity_name: str = Field(..., description="Amenity to analyze")
    optimization_focus: str = Field("All Resources", description="Electricity, Water, Gas, All Resources, Cost Savings, Environmental Impact")

class EnergyTrackingResponse(BaseModel):
    amenity_name: str
    optimization_focus: str
    total_electricity: float
    total_water: float
    total_gas: float
    total_cost: float
    electricity_per_guest: float
    water_per_guest: float
    cost_per_guest: float
    ai_analysis: str
    optimization_suggestions: List[str]



class FoodForecastRequest(BaseModel):
    item_name: str = Field(..., description="Food item to forecast")
    forecast_type: str = Field("Days", description="Days, Months, or Years")
    forecast_value: int = Field(7, description="Number of units to forecast")

class FoodForecastResponse(BaseModel):
    item_name: str
    forecast_type: str
    forecast_days: int
    actual_data: List[Dict[str, Any]]
    forecast_data: List[Dict[str, Any]]

class FoodReorderRequest(BaseModel):
    item_name: str = Field(..., description="Food item to reorder")
    quantity: int = Field(..., description="Quantity to reorder")

class FoodReorderResponse(BaseModel):
    item_name: str
    requested_quantity: int
    available_stock: float
    ai_response: str


from pydantic import BaseModel, Field
from typing import List, Dict, Any

class FeedbackAnalysisRequest(BaseModel):
    branch: str = Field("All", description="Branch ID or 'All'")
    category: str = Field("All", description="Feedback category or 'All'")

class FeedbackAnalysisResponse(BaseModel):
    branch: str
    category: str
    feedback_count: int
    ai_analysis: str

class FinancialForecastRequest(BaseModel):
    forecast_unit: str = Field("Months", description="Months or Years")
    forecast_period: int = Field(6, description="Number of units to forecast")

class FinancialForecastResponse(BaseModel):
    income_forecast: List[Dict[str, Any]]
    expense_forecast: List[Dict[str, Any]]
    profit_loss_forecast: List[Dict[str, Any]]

class BranchDemandRequest(BaseModel):
    branch_name: str = Field(..., description="Name of the branch to analyze")
    forecast_days: int = Field(7, description="Number of days to forecast (1-30)")

class BranchDemandResponse(BaseModel):
    branch_name: str
    forecast_days: int
    forecast_data: List[Dict[str, Any]]
    ai_analysis: str
    status: str

from pydantic import BaseModel
from typing import Optional


class LoginRequest(BaseModel):
    password: str


class LoginResponse(BaseModel):
    success: bool
    message: str


class QueryRequest(BaseModel):
    question: str
    selected_hotel: str = "All Hotels"
    previous_question: Optional[str] = ""
    previous_answer: Optional[str] = ""
    translate_answer_to: Optional[str] = None
    user_type: str = "guest"  # "guest" or "client"
    password: Optional[str] = None


class QueryResponse(BaseModel):
    answer: str
    selected_hotel: str
    translated_answer: Optional[str] = None


class BillingForecastRequest(BaseModel):
    """Request for next-day billing prediction. Client password required."""
    password: str
    days: Optional[int] = 7  # Use last N days of charges to predict next day


class BillingForecastResponse(BaseModel):
    predicted_amount: float
    based_on_days: int
    message: str


class TranslateRequest(BaseModel):
    text: str
    target_language: str


class TranslateResponse(BaseModel):
    original_text: str
    translated_text: str
    target_language: str


class SpeechQueryResponse(BaseModel):
    detected_language: str
    original_text: str
    english_question: str
    answer: str
    translated_answer: Optional[str] = None
    selected_hotel: str


class MaintenanceInput(BaseModel):
    room_id: str
    last_maintenance_date: Optional[str] = None
    equipment_age_days: Optional[int] = 0
    recent_issues: Optional[str] = ""

class HousekeepingInput(BaseModel):
    room_id: str
    occupancy_status: str
    last_cleaned: Optional[str] = None
    guest_checkout_time: Optional[str] = None

class CleaningInput(BaseModel):
    room_id: str
    cleaning_score: Optional[float] = 0.0
    feedback: Optional[str] = ""
    areas_needing_attention: Optional[str] = ""

class GuestRequest(BaseModel):
    full_name: str
    phone_number: Optional[str] = None

class GuestAnalysisResponse(BaseModel):
    status: str
    guest: str
    analysis: str
    analysis_json: Optional[List[Dict[str, Any]]] = None
    guest_data: Dict[str, Any]
    row_data: Dict[str, Any]
    visit_count: Optional[int] = None
    summary: Dict[str, Any]


# ----------------------------
# Fraud Alerts POS Models
# ----------------------------

class OrderRecord(BaseModel):
    id: Optional[int] = None
    waiterId: Optional[Any] = None
    status: Optional[str] = None
    created: Optional[str] = None
    timestamp: Optional[str] = None
    # Allow extra fields for flexibility
    extras: Optional[Dict[str, Any]] = None


class PaymentRecord(BaseModel):
    orderId: Optional[Any] = None
    waiterId: Optional[Any] = None
    notes: Optional[str] = None
    amount: Optional[float] = None
    created: Optional[str] = None
    extras: Optional[Dict[str, Any]] = None


class WOLogRecord(BaseModel):
    action: Optional[str] = None
    changedBy: Optional[Any] = None
    created: Optional[str] = None
    timestamp: Optional[str] = None
    extras: Optional[Dict[str, Any]] = None


class WaiterRecord(BaseModel):
    id: Optional[Any] = None
    name: Optional[str] = None
    extras: Optional[Dict[str, Any]] = None


class EmployeeSummaryItem(BaseModel):
    id: str
    name: str
    cancellations: int = 0
    payment_misuse: int = 0
    data_leak: int = 0


class EmployeeRiskResponse(BaseModel):
    ai_output: Optional[str]
    ai_result: Optional[Dict[str, Any]]
    employee_summary: List[EmployeeSummaryItem]


class DailySummaryResponse(BaseModel):
    summary: Dict[str, Any]
    ai_output: Optional[str]
    ai_result: Optional[Dict[str, Any]]



class ReorderRequest(BaseModel):
    material: str
    use_ai: bool | None = True


class ReorderResponse(BaseModel):
    material: str = Field(..., description="Material or item code")
    reorder_required: bool = Field(..., description="Whether reorder is required")
    recommended_quantity: int = Field(..., description="Suggested reorder quantity")
    current_stock: Optional[int] = Field(None, description="Current available stock")
    threshold_stock: Optional[int] = Field(None, description="Minimum threshold stock")
    ai_analysis: Optional[str] = Field(None, description="AI-generated explanation")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional info")