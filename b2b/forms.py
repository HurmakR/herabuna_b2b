# b2b/forms.py
from django import forms
import re
from django.core.validators import RegexValidator
from django.contrib.auth import get_user_model, authenticate
from django.forms import formset_factory

from .models import Address, Product
from django.contrib.auth.forms import AuthenticationForm

Dealer = get_user_model()

phone_validator = RegexValidator(
    regex=r"^380\d{9}$",
    message="Введіть номер у форматі 380XXXXXXXXX.",
)

# -------- Signup --------
class DealerSignUpForm(forms.ModelForm):
    password1 = forms.CharField(label="Пароль", widget=forms.PasswordInput, min_length=8)
    password2 = forms.CharField(label="Підтвердіть пароль", widget=forms.PasswordInput)
    phone = forms.CharField(
        label="Телефон", max_length=12, validators=[phone_validator],
        help_text="Формат: 380XXXXXXXXX",
        widget=forms.TextInput(attrs={"placeholder": "380XXXXXXXXX", "inputmode": "numeric", "pattern": r"380\d{9}"}),
    )
    class Meta:
        model = Dealer
        fields = ["username", "email", "company_name", "phone", "first_name", "last_name"]
        labels = {"username": "Логін", "email": "Email", "company_name": "Компанія / Магазин", "first_name": "Ім'я", "last_name": "Прізвище"}

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "Паролі не співпадають.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
        return user


# -------- Profile --------
class ProfileForm(forms.ModelForm):
    phone = forms.CharField(
        label="Телефон", max_length=12, validators=[phone_validator],
        widget=forms.TextInput(attrs={"placeholder": "380XXXXXXXXX", "inputmode": "numeric", "pattern": r"380\d{9}"}),
    )
    class Meta:
        model = Dealer
        fields = ["first_name", "last_name", "company_name", "email", "phone"]
        labels = {"first_name": "Ім'я", "last_name": "Прізвище", "company_name": "Компанія / Магазин", "email": "Email", "phone": "Телефон"}


# -------- Адреси --------
class AddressForm(forms.ModelForm):
    recipient_phone = forms.CharField(
        label="Телефон отримувача", max_length=12, validators=[phone_validator],
        widget=forms.TextInput(attrs={"placeholder": "380XXXXXXXXX", "inputmode": "numeric", "pattern": r"380\d{9}"}),
    )
    class Meta:
        model = Address
        fields = ["title", "city_name", "city_ref", "warehouse_name", "warehouse_ref", "recipient_name", "recipient_phone", "is_default"]
        labels = {"title": "Назва адреси", "city_name": "Місто", "warehouse_name": "Відділення", "recipient_name": "Отримувач", "recipient_phone": "Телефон отримувача", "is_default": "За замовчуванням"}
        widgets = {
            "city_name": forms.TextInput(attrs={"data-np-city": "1", "autocomplete": "off"}),
            "warehouse_name": forms.TextInput(attrs={"data-np-warehouse": "1", "autocomplete": "off"}),
        }


class CustomAuthenticationForm(AuthenticationForm):
    error_messages = {
        "invalid_login": "Невірний логін або пароль.",
        "inactive": "Ваш акаунт ще не активовано. Дочекайтеся підтвердження від адміністратора.",
    }

    def clean(self):
        username = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")

        if username and password:
            # First check if user exists and password is correct but account inactive
            try:
                user_obj = Dealer.objects.get(username=username)
                if user_obj.check_password(password) and not user_obj.is_active:
                    raise forms.ValidationError(
                        self.error_messages["inactive"],
                        code="inactive",
                    )
            except Dealer.DoesNotExist:
                pass

            # Normal authentication flow
            self.user_cache = authenticate(self.request, username=username, password=password)
            if self.user_cache is None:
                raise forms.ValidationError(
                    self.error_messages["invalid_login"],
                    code="invalid_login",
                    params={"username": self.username_field.verbose_name},
                )
            else:
                self.confirm_login_allowed(self.user_cache)

        return self.cleaned_data


# -------- Staff: create order --------
class AdminOrderCreateForm(forms.Form):
    dealer = forms.ModelChoiceField(
        label="Клієнт",
        queryset=Dealer.objects.filter(is_dealer=True).order_by("username"),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    note = forms.CharField(label="Примітка", required=False, widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}))


class AdminOrderLineForm(forms.Form):
    sku = forms.ChoiceField(
        label="Товар", required=False, choices=[("", "— виберіть товар —")],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    qty = forms.IntegerField(
        label="К-сть", required=False, min_value=1,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "1", "min": "1", "inputmode": "numeric"}),
    )

    def __init__(self, *args, product_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        if product_choices is not None:
            self.fields["sku"].choices = product_choices
        else:
            qs = Product.objects.filter(is_active=True, wholesale_price__gt=0, stock_qty__gt=0).order_by("name")
            self.fields["sku"].choices = [("", "— виберіть товар —"), *[(p.sku, f"{p.sku} — {p.name} (залишок {p.stock_qty})") for p in qs]]


AdminOrderLineFormSet = formset_factory(AdminOrderLineForm, extra=1, can_delete=True)


class OrderShippingForm(forms.Form):
    shipping_city = forms.CharField(label='Місто (НП)', required=True)
    shipping_city_ref = forms.CharField(required=True, widget=forms.HiddenInput)
    shipping_warehouse = forms.CharField(label='Відділення (НП)', required=True)
    shipping_warehouse_ref = forms.CharField(required=True, widget=forms.HiddenInput)
    shipping_recipient = forms.CharField(label='Одержувач', required=True)
    shipping_phone = forms.CharField(
        label='Телефон одержувача', required=True, max_length=32,
        widget=forms.TextInput(attrs={'placeholder': '380XXXXXXXXX', 'inputmode': 'numeric', 'pattern': r'380\d{9}'}),
        help_text='Формат: 380XXXXXXXXX (без +, пробілів і дужок).',
    )

    def clean_shipping_phone(self):
        raw = self.cleaned_data.get('shipping_phone')
        s = str(raw or '').strip()
        digits = re.sub(r'\D+', '', s)
        if not digits:
            raise forms.ValidationError('Вкажіть телефон у форматі 380XXXXXXXXX.')
        if len(digits) == 10 and digits.startswith('0'):
            digits = '38' + digits
        if len(digits) == 11 and digits.startswith('80'):
            digits = '3' + digits
        if not re.fullmatch(r'380\d{9}', digits):
            raise forms.ValidationError('Введіть номер у форматі 380XXXXXXXXX (без +, пробілів і дужок).')
        return digits


class OrderTTNForm(forms.Form):
    shipping_ttn = forms.CharField(
        label='ТТН', required=True, max_length=64,
        widget=forms.TextInput(attrs={'placeholder': '2045... або інший номер ТТН', 'autocomplete': 'off'}),
    )

    def clean_shipping_ttn(self):
        return str(self.cleaned_data.get('shipping_ttn') or '').strip()
