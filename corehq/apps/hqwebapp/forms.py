from crispy_forms import layout as crispy
from crispy_forms.bootstrap import StrictButton
from crispy_forms.helper import FormHelper
from django.contrib.auth.forms import AuthenticationForm
from django import forms
from django.utils.translation import ugettext_lazy as _


class EmailAuthenticationForm(AuthenticationForm):
    username = forms.EmailField(label=_("E-mail"), max_length=75)

    def clean_username(self):
        username = self.cleaned_data['username'].lower()
        return username


class CloudCareAuthenticationForm(EmailAuthenticationForm):
    username = forms.EmailField(label=_("Username"), max_length=75)


class BulkUploadForm(forms.Form):
    bulk_upload_file = forms.FileField(label="")
    action = forms.CharField(widget=forms.HiddenInput(), initial='bulk_upload')

    def __init__(self, *args, **kwargs):
        super(BulkUploadForm, self).__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_id = 'bulk_upload_form'
        self.helper.form_method = 'post'
        self.helper.layout = crispy.Layout(
            crispy.Fieldset(
                "",
                crispy.Field(
                    'bulk_upload_file',
                    data_bind="value: file",
                ),
                crispy.Field(
                    'action',
                ),
            ),
            StrictButton(
                '<i class="icon-cloud-upload"></i> Upload Cases',
                css_class='btn btn-primary',
                data_bind='visible: file()',
                type='submit',
            ),
        )
