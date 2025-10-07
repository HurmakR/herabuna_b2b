from django import forms
from django.contrib.auth import get_user_model
from .models import Address, Dealer
from django.contrib.auth.forms import UserCreationForm

Dealer = get_user_model()
class DealerSignUpForm(UserCreationForm):
    class Meta:
        model = Dealer
        fields = ("username", "company_name", "email", "phone", "billing_address", "shipping_address")


class ProfileForm(forms.ModelForm):
    """Basic dealer profile form."""
    class Meta:
        model = Dealer
        fields = ["username", "email", "company_name", "phone", "telegram_chat_id"]
        widgets = {
            "username": forms.TextInput(attrs={"readonly": "readonly"}),
        }

class AddressForm(forms.ModelForm):
    """Nova Poshta warehouse destination form."""
    class Meta:
        model = Address
        fields = [
            "title",
            "city_name", "city_ref",
            "warehouse_name", "warehouse_ref",
            "recipient_name", "recipient_phone",
            "is_default",
        ]
        help_texts = {
            "city_ref": "Nova Poshta city Ref (GUID).",
            "warehouse_ref": "Nova Poshta warehouse Ref (GUID).",
        }

    def clean(self):
        cleaned = super().clean()
        # Minimal NP validation: both refs must be set together
        if bool(cleaned.get("city_ref")) ^ bool(cleaned.get("warehouse_ref")):
            raise forms.ValidationError("Заповніть обидва поля: city_ref та warehouse_ref.")
        return cleaned
