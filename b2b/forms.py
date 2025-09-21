from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import Dealer

class DealerSignUpForm(UserCreationForm):
    """Dealer self-registration form (can be auto-approved or reviewed by admin)."""
    class Meta:
        model = Dealer
        fields = ('username', 'company_name', 'email', 'phone', 'billing_address', 'shipping_address')
