import json
import io
import zipfile
import csv

from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mass_mail
from django.core.urlresolvers import reverse
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.contrib.admin.views.decorators import staff_member_required
from django.forms.models import model_to_dict
from blti import lti_provider

from .forms import SettingsForm, getSubmissionForm, SubmissionFileUpdateForm, MailForm
from .models import SubmissionFile, Submission, Assignment, TestMachine, Course, UserProfile
from .models.userprofile import db_fixes, move_user_data
from .models.course import lti_secret
from .settings import MAIN_URL
from .social import passthrough

import logging
logger = logging.getLogger('OpenSubmit')


@login_required
def new(request, ass_id):
    ass = get_object_or_404(Assignment, pk=ass_id)

    # Check whether submissions are allowed.
    if not ass.can_create_submission(user=request.user):
        raise PermissionDenied(
            "You are not allowed to create a submission for this assignment")

    # get submission form according to the assignment type
    SubmissionForm = getSubmissionForm(ass)

    # Analyze submission data
    if request.POST:
        # we need to fill all forms here,
        # so that they can be rendered on validation errors
        submissionForm = SubmissionForm(request.user,
                                        ass,
                                        request.POST,
                                        request.FILES)
        if submissionForm.is_valid():
            # commit=False to set submitter in the instance
            submission = submissionForm.save(commit=False)
            submission.submitter = request.user
            submission.assignment = ass
            submission.state = submission.get_initial_state()
            # take uploaded file from extra field
            if ass.has_attachment:
                upload_file = request.FILES['attachment']
                submissionFile = SubmissionFile(
                    attachment=submissionForm.cleaned_data['attachment'],
                    original_filename=upload_file.name)
                submissionFile.save()
                submission.file_upload = submissionFile
            submission.save()
            # because of commit=False, we first need to add
            # the form-given authors
            submissionForm.save_m2m()
            submission.save()
            messages.info(request, "New submission saved.")
            if submission.state == Submission.SUBMITTED:
                # Initial state is SUBMITTED,
                # which means that there is no validation
                if not submission.assignment.is_graded():
                    # No validation, no grading. We are done.
                    submission.state = Submission.CLOSED
                    submission.save()
            return redirect('dashboard')
        else:
            messages.error(
                request, "Please correct your submission information.")
    else:
        submissionForm = SubmissionForm(request.user, ass)
    return render(request, 'new.html', {'submissionForm': submissionForm,
                                        'assignment': ass})


@login_required
def update(request, subm_id):
    # Submission should only be editable by their creators
    submission = get_object_or_404(Submission, pk=subm_id)
    # Somebody may bypass the template check by sending direct POST form data.
    # This check also fails when the student performs a simple reload on the /update page
    # without form submission after the deadline.
    # In practice, the latter happens all the time, so we skip the Exception raising here,
    # which lead to endless mails for the admin user in the past.
    if not submission.can_reupload():
        return redirect('dashboard')
        #raise SuspiciousOperation("Update of submission %s is not allowed at this time." % str(subm_id))
    if request.user not in submission.authors.all():
        return redirect('dashboard')
    if request.POST:
        updateForm = SubmissionFileUpdateForm(request.POST, request.FILES)
        if updateForm.is_valid():
            upload_file = request.FILES['attachment']
            new_file = SubmissionFile(
                attachment=updateForm.files['attachment'],
                original_filename=upload_file.name)
            new_file.save()
            # fix status of old uploaded file
            submission.file_upload.replaced_by = new_file
            submission.file_upload.save()
            # store new file for submissions
            submission.file_upload = new_file
            submission.state = submission.get_initial_state()
            submission.notes = updateForm.data['notes']
            submission.save()
            messages.info(request, 'Submission files successfully updated.')
            return redirect('dashboard')
    else:
        updateForm = SubmissionFileUpdateForm(instance=submission)
    return render(request, 'update.html', {'submissionFileUpdateForm': updateForm,
                                           'submission': submission})


@login_required
@staff_member_required
def mergeusers(request):
    '''
        Offers an intermediate admin view to merge existing users.
    '''
    if request.method == 'POST':
        primary = get_object_or_404(User, pk=request.POST['primary_id'])
        secondary = get_object_or_404(User, pk=request.POST['secondary_id'])
        try:
            move_user_data(primary, secondary)
            messages.info(request, 'Submissions moved to user %u.' %
                          (primary.pk))
        except:
            messages.error(
                request, 'Error during data migration, nothing changed.')
            return redirect('admin:index')
        messages.info(request, 'User %u deleted.' % (secondary.pk))
        secondary.delete()
        return redirect('admin:index')
    primary = get_object_or_404(User, pk=request.GET['primary_id'])
    secondary = get_object_or_404(User, pk=request.GET['secondary_id'])
    # Determine data to be migrated
    return render(request, 'mergeusers.html', {'primary': primary, 'secondary': secondary})


@login_required
@staff_member_required
def preview(request, subm_id):
    '''
        Renders a preview of the uploaded student file.
        This is only intended for the grading procedure, so staff status is needed.
    '''
    submission = get_object_or_404(Submission, pk=subm_id)
    return render(request, 'file_preview.html', {'submission': submission})


@login_required
@staff_member_required
def perftable(request, ass_id):
    assignment = get_object_or_404(Assignment, pk=ass_id)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="perf_assignment%u.csv"' % assignment.pk
    writer = csv.writer(response, delimiter=';')
    writer.writerow(['Assignment', 'Submission ID',
                     'Authors', 'Performance Data'])
    for sub in assignment.submissions.all():
        result = sub.get_fulltest_result()
        if result:
            row = [str(sub.assignment), str(sub.pk), ", ".join(sub.authors.values_list(
                'username', flat=True).order_by('username')), str(result.perf_data)]
            writer.writerow(row)
    return response


@login_required
@staff_member_required
def duplicates(request, ass_id):
    ass = get_object_or_404(Assignment, pk=ass_id)
    return render(request, 'duplicates.html', {
        'duplicates': ass.duplicate_files(),
        'assignment': ass
    })


@login_required
@staff_member_required
def gradingtable(request, course_id):
    author_submissions = {}
    course = get_object_or_404(Course, pk=course_id)
    assignments = course.assignments.all().order_by('title')
    # find all gradings per author and assignment
    for assignment in assignments:
        for submission in assignment.submissions.all().filter(state=Submission.CLOSED):
            for author in submission.authors.all():
                # author_submissions is a dict mapping authors to another dict
                # This second dict maps assignments to submissions (for this author)
                # A tuple as dict key does not help here, since we want to iterate over the assignments later
                if author not in list(author_submissions.keys()):
                    author_submissions[author] = {assignment.pk: submission}
                else:
                    author_submissions[author][assignment.pk] = submission
    resulttable = []
    for author, ass2sub in list(author_submissions.items()):
        columns = []
        numpassed = 0
        numgraded = 0
        pointsum = 0
        columns.append(author.last_name if author.last_name else '')
        columns.append(author.first_name if author.first_name else '')
        columns.append(
            author.profile.student_id if author.profile.student_id else '')
        columns.append(
            author.profile.study_program if author.profile.study_program else '')
        # Process all assignments in the table order, once per author (loop above)
        for assignment in assignments:
            if assignment.pk in ass2sub:
                # Ok, we have a submission for this author in this assignment
                submission = ass2sub[assignment.pk]
                if assignment.is_graded():
                    # is graded, make part of statistics
                    numgraded += 1
                    if submission.grading_means_passed():
                        numpassed += 1
                        try:
                            pointsum += int(str(submission.grading))
                        except Exception:
                            pass
                # considers both graded and ungraded assignments
                columns.append(submission.grading_value_text())
            else:
                # No submission for this author in this assignment
                # This may or may not be bad, so we keep it neutral here
                columns.append('-')
        columns.append("%s / %s" % (numpassed, numgraded))
        columns.append("%u" % pointsum)
        resulttable.append(columns)
    return render(request, 'gradingtable.html',
                  {'course': course, 'assignments': assignments,
                   'resulttable': sorted(resulttable)})


def _mail_form(request, users_qs):
    receivers_qs = users_qs.order_by('email').distinct().values('first_name', 'last_name', 'email')
    receivers = [receiver for receiver in receivers_qs]
    request.session['mail_receivers'] = receivers
    return render(request, 'mail_form.html', {'receivers': receivers, 'mailform': MailForm()})


@login_required
@staff_member_required
def mail_course(request, course_id):
    course = get_object_or_404(Course, pk=course_id)
    users = User.objects.filter(profile__courses__pk=course.pk)
    if users.count() == 0:
        messages.warning(request, 'No students in this course.')
        return redirect('teacher:index')
    else:
        return _mail_form(request, users)


@login_required
@staff_member_required
def mail_students(request, student_ids):
    id_list = [int(val) for val in student_ids.split(',')]
    users = User.objects.filter(pk__in=id_list).distinct()
    return _mail_form(request, users)


@login_required
@staff_member_required
def mail_preview(request):
    def _replace_placeholders(text, user):
        return text.replace("#FIRSTNAME#", user['first_name'].strip()) \
                   .replace("#LASTNAME#", user['last_name'].strip())

    if 'mail_receivers' in request.session and \
       'subject' in request.POST and \
       'message' in request.POST:
        data = [{'subject': _replace_placeholders(request.POST['subject'], receiver),
                 'message': _replace_placeholders(request.POST['message'], receiver),
                 'to': receiver['email']
                 } for receiver in request.session['mail_receivers']]

        request.session['mail_data'] = data
        del request.session['mail_receivers']
        return render(request, 'mail_preview.html', {'data': data})
    else:
        messages.error(request, 'Error while rendering mail preview.')
        return redirect('teacher:index')


@login_required
@staff_member_required
def mail_send(request):
    if 'mail_data' in request.session:
        tosend = [[d['subject'],
                   d['message'],
                   request.user.email,
                   [d['to']]]
                  for d in request.session['mail_data']]
        sent = send_mass_mail(tosend, fail_silently=True)
        messages.add_message(request, messages.INFO,
                             '%u message(s) sent.' % sent)
        del request.session['mail_data']
    else:
        messages.error(request, 'Error while preparing mail sending.')
    return redirect('teacher:index')


@login_required
@staff_member_required
def coursearchive(request, course_id):
    '''
        Provides all course submissions and their information as archive download.
        For archiving purposes, since withdrawn submissions are included.
    '''
    output = io.BytesIO()
    z = zipfile.ZipFile(output, 'w')

    course = get_object_or_404(Course, pk=course_id)
    assignments = course.assignments.order_by('title')
    for ass in assignments:
        ass.add_to_zipfile(z)
        subs = ass.submissions.all().order_by('submitter')
        for sub in subs:
            sub.add_to_zipfile(z)

    z.close()
    # go back to start in ZIP file so that Django can deliver it
    output.seek(0)
    response = HttpResponse(
        output, content_type="application/x-zip-compressed")
    response['Content-Disposition'] = 'attachment; filename=%s.zip' % course.directory_name()
    return response


@login_required
@staff_member_required
def assarchive(request, ass_id):
    '''
        Provides all non-withdrawn submissions for an assignment as download.
        Intented for supporting offline correction.
    '''
    output = io.BytesIO()
    z = zipfile.ZipFile(output, 'w')

    ass = get_object_or_404(Assignment, pk=ass_id)
    ass.add_to_zipfile(z)
    subs = Submission.valid_ones.filter(assignment=ass).order_by('submitter')
    for sub in subs:
        sub.add_to_zipfile(z)

    z.close()
    # go back to start in ZIP file so that Django can deliver it
    output.seek(0)
    response = HttpResponse(
        output, content_type="application/x-zip-compressed")
    response['Content-Disposition'] = 'attachment; filename=%s.zip' % ass.directory_name()
    return response



@login_required
def withdraw(request, subm_id):
    # submission should only be deletable by their creators
    submission = get_object_or_404(Submission, pk=subm_id)
    if not submission.can_withdraw(user=request.user):
        raise PermissionDenied(
            "Withdrawal for this assignment is no longer possible, or you are unauthorized to access that submission.")
    if "confirm" in request.POST:
        submission.state = Submission.WITHDRAWN
        submission.save()
        messages.info(request, 'Submission successfully withdrawn.')
        return redirect('dashboard')
    else:
        return render(request, 'withdraw.html', {'submission': submission})


@lti_provider(consumer_lookup=lti_secret, site_url=MAIN_URL)
@require_POST
def lti(request, post_params, consumer_key, *args, **kwargs):
    '''
        Entry point for LTI consumers.

        This view is protected by the BLTI package decorator,
        which performs all the relevant OAuth signature checking.
        It also makes sure that the LTI consumer key and secret were ok.
        The latter ones are supposed to be configured in the admin interface.

        We can now trust on the provided data to be from the LTI provider.

        If everything worked out, we store the information the session for
        the Python Social passthrough provider, which is performing
        user creation and database storage.
    '''
    data = {}
    data['ltikey'] = post_params.get('oauth_consumer_key')
    # None of them is mandatory
    data['id'] = post_params.get('user_id', None)
    data['username'] = post_params.get('custom_username', None)
    data['last_name'] = post_params.get('lis_person_name_family', None)
    data['email'] = post_params.get('lis_person_contact_email_primary', None)
    data['first_name'] = post_params.get('lis_person_name_given', None)
    request.session[passthrough.SESSION_VAR] = data
    return redirect(reverse('social:begin', args=['lti']))
