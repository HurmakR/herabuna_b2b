from django import forms


class ConfirmActionForm(forms.Form):
    """Simple confirmation form for destructive actions."""

    confirm = forms.CharField(
        label="Підтвердження",
        help_text="Введіть RESET для підтвердження",
        max_length=32,
        required=True,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "RESET",
                "autocomplete": "off",
            }
        ),
    )

    def clean_confirm(self):
        value = (self.cleaned_data.get("confirm") or "").strip().upper()
        if value != "RESET":
            raise forms.ValidationError("Потрібно ввести RESET для підтвердження.")
        return value


class ImportBackupForm(forms.Form):
    """Upload JSON fixture and optionally clear data before import."""

    CLEAR_CHOICES = [
        ("none", "Не очищати (додати до існуючих)"),
        ("warehouse", "Очистити тільки склад (лоти/рухи/резерви/накладні)"),
        ("orders", "Очистити замовлення + склад (каталог лишається)"),
    ]

    file = forms.FileField(
        label="Файл бекапу (JSON)",
        required=True,
        widget=forms.ClearableFileInput(attrs={"class": "form-control"}),
    )
    clear_scope = forms.ChoiceField(
        label="Перед імпортом",
        choices=CLEAR_CHOICES,
        required=True,
        initial="warehouse",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    confirm = forms.CharField(
        label="Підтвердження",
        help_text="Введіть RESET, якщо обрали очищення",
        required=False,
        max_length=32,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "RESET (тільки якщо очищення)",
                "autocomplete": "off",
            }
        ),
    )

    def clean(self):
        cleaned = super().clean()
        scope = cleaned.get("clear_scope")
        confirm = (cleaned.get("confirm") or "").strip().upper()
        if scope in {"warehouse", "orders"} and confirm != "RESET":
            raise forms.ValidationError("Для очищення перед імпортом потрібно ввести RESET.")
        return cleaned
