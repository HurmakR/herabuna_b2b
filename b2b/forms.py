from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import Dealer, Address

class DealerSignUpForm(UserCreationForm):
    email = forms.EmailField(label="Email", required=True)
    company_name = forms.CharField(label="Компанія / Магазин", max_length=255, required=False)
    phone = forms.CharField(label="Телефон", max_length=50, required=False)

    class Meta(UserCreationForm.Meta):
        model = Dealer
        fields = ("username", "email", "company_name", "phone")  # password1/2 додаються базовим класом
        labels = {
            "username": "Логін",
        }

class ProfileForm(forms.ModelForm):
    class Meta:
        model = Dealer
        fields = ("email", "company_name", "phone", "first_name", "last_name")
        labels = {
            "email": "Email",
            "company_name": "Компанія / Магазин",
            "phone": "Телефон",
            "first_name": "Ім’я",
            "last_name": "Прізвище",
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