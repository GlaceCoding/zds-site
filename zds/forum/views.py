#!/usr/bin/python
# -*- coding: utf-8 -*-
from datetime import datetime
import json

from django.conf import settings
from django.db.models import Q
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.mail import EmailMultiAlternatives
from django.core.paginator import Paginator, PageNotAnInteger, EmptyPage
from django.core.urlresolvers import reverse
from django.db import transaction
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, get_object_or_404, render, render_to_response
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST
from django.utils.translation import ugettext_lazy as _
from django.views.generic import ListView, DetailView
from django.views.generic.detail import SingleObjectMixin

from haystack.inputs import AutoQuery
from haystack.query import SearchQuerySet

from forms import TopicForm, PostForm, MoveTopicForm
from models import Category, Forum, Topic, Post, follow, follow_by_email, never_read, \
    mark_read, TopicFollowed
from zds.forum.models import TopicRead
from zds.member.decorator import can_write_and_read_now
from zds.member.views import get_client_ip
from zds.utils import slugify
from zds.utils.mixins import FilterMixin
from zds.utils.models import Alert, CommentLike, CommentDislike, Tag
from zds.utils.mps import send_mp
from zds.utils.paginator import paginator_range, ZdSPagingListView
from zds.utils.templatetags.emarkdown import emarkdown


class CategoriesForumsListView(ListView):

    context_object_name = 'categories'
    template_name = 'forum/index.html'
    queryset = Category.objects.all()

    def get_context_data(self, **kwargs):
        context = super(CategoriesForumsListView, self).get_context_data(**kwargs)
        for category in context.get('categories'):
            category.forums = category.get_forums(self.request.user)
        return context


class CategoryForumsDetailView(DetailView):

    context_object_name = 'category'
    template_name = 'forum/category/index.html'
    queryset = Category.objects.all()

    def get_context_data(self, **kwargs):
        context = super(CategoryForumsDetailView, self).get_context_data(**kwargs)
        context['forums'] = context.get('category').get_forums(self.request.user)
        return context


class TopicsListView(FilterMixin, ZdSPagingListView, SingleObjectMixin):

    context_object_name = 'topics'
    paginate_by = settings.ZDS_APP['forum']['topics_per_page']
    template_name = 'forum/category/forum.html'
    queryset = Topic.objects.filter(is_sticky=False).all()
    filter_url_kwarg = 'filter'
    default_filter_param = 'all'
    object = None

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.object.can_read(self.request.user):
            raise PermissionDenied
        return super(TopicsListView, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(TopicsListView, self).get_context_data(**kwargs)
        context.update({
            'forum': self.object,
            'sticky_topics': self.filter_queryset(Topic.objects.filter(is_sticky=True).all(), context['filter']),
        })
        return context

    def get_object(self, queryset=None):
        return get_object_or_404(Forum, slug=self.kwargs.get('forum_slug'))

    def filter_queryset(self, queryset, filter_param):
        if filter_param == 'solve':
            queryset = queryset.filter(forum__pk=self.object.pk, is_solved=True)
        elif filter_param == 'unsolve':
            queryset = queryset.filter(forum__pk=self.object.pk, is_solved=False)
        elif filter_param == 'noanswer':
            queryset = queryset.filter(forum__pk=self.object.pk, last_message__position=1)
        else:
            queryset = queryset.filter(forum__pk=self.object.pk)
        return queryset\
            .order_by('-last_message__pubdate')\
            .select_related('author__profile')\
            .prefetch_related('last_message', 'tags').all()


def topic(request, topic_pk, topic_slug):
    """
    Displays a topic (if the user can read it) and all its posts, paginated with
    `settings.ZDS_APP['forum']['posts_per_page']` posts per page.
    If the page to display is not the first one, the last post of the previous page is displayed as the first post of
    the current page.
    :param topic_pk: The primary key of the topic to display
    :param topic_slug: The slug of the topic to display. If the slug doesn't match the topic slug, the user is
    redirected to the proper topic URL.
    """

    topic = get_object_or_404(Topic, pk=topic_pk)
    if not topic.forum.can_read(request.user):
        raise PermissionDenied

    # Check link

    if not topic_slug == slugify(topic.title):
        return redirect(topic.get_absolute_url())

    # If the user is authenticated and has never read topic, we mark it as
    # read.

    if request.user.is_authenticated():
        if never_read(topic):
            mark_read(topic)

    # Retrieves all posts of the topic and use paginator with them.

    posts = \
        Post.objects.filter(topic__pk=topic.pk) \
        .select_related("author__profile") \
        .order_by("position").all()
    last_post_pk = topic.last_message.pk

    # Handle pagination

    paginator = Paginator(posts, settings.ZDS_APP['forum']['posts_per_page'])

    # The category list is needed to move threads

    categories = Category.objects.all()
    if "page" in request.GET:
        try:
            page_nbr = int(request.GET["page"])
        except (KeyError, ValueError):
            # problem in variable format
            raise Http404
    else:
        page_nbr = 1
    try:
        posts = paginator.page(page_nbr)
    except PageNotAnInteger:
        posts = paginator.page(1)
    except EmptyPage:
        raise Http404
    res = []
    if page_nbr != 1:

        # Show the last post of the previous page

        last_page = paginator.page(page_nbr - 1).object_list
        last_post = last_page[len(last_page) - 1]
        res.append(last_post)
    for post in posts:
        res.append(post)

    # Build form to send a post for the current topic.

    form = PostForm(topic, request.user)
    form.helper.form_action = reverse("zds.forum.views.answer") + "?sujet=" \
        + str(topic.pk)
    form_move = MoveTopicForm(topic=topic)

    return render(request, "forum/topic/index.html", {
        "topic": topic,
        "posts": res,
        "categories": categories,
        "page": 1,
        "pages": paginator_range(page_nbr, paginator.num_pages),
        "nb": page_nbr,
        "last_post_pk": last_post_pk,
        "form": form,
        "form_move": form_move,
    })


def get_tag_by_title(title):
    """
    Extract tags from title.
    In a title, tags can be set this way:
    > [Tag 1][Tag 2] There is the real title
    Rules to detect tags:
    - Tags are enclosed in square brackets. This allows multi-word tags instead of hashtags.
    - Tags can embed square brackets: [Tag] is a valid tag and must be written [[Tag]] in the raw title
    - All tags must be declared at the beginning of the title. Example: _"Title [tag]"_ will not create a tag.
    - Tags and title correctness (example: empty tag/title detection) is **not** checked here
    :param title: The raw title
    :return: A tuple: (the tag list, the title without the tags).
    """
    nb_bracket = 0
    current_tag = u""
    current_title = u""
    tags = []
    continue_parsing_tags = True
    original_title = title
    for char in title:

        if char == u"[" and nb_bracket == 0 and continue_parsing_tags:
            nb_bracket += 1
        elif nb_bracket > 0 and char != u"]" and continue_parsing_tags:
            current_tag = current_tag + char
            if char == u"[":
                nb_bracket += 1
        elif char == u"]" and nb_bracket > 0 and continue_parsing_tags:
            nb_bracket -= 1
            if nb_bracket == 0 and current_tag.strip() != u"":
                tags.append(current_tag.strip())
                current_tag = u""
            elif current_tag.strip() != u"" and nb_bracket > 0:
                current_tag = current_tag + char

        elif (char != u"[" and char.strip() != "") or not continue_parsing_tags:
            continue_parsing_tags = False
            current_title = current_title + char
    title = current_title
    # if we did not succed in parsing the tags
    if nb_bracket != 0:
        return ([], original_title)

    return (tags, title.strip())


@can_write_and_read_now
@login_required
@transaction.atomic
def new(request):
    """
    Creates a new topic in a forum.
    """

    try:
        forum_pk = int(request.GET["forum"])
    except (KeyError, ValueError):
        # problem in variable format
        raise Http404
    forum = get_object_or_404(Forum, pk=forum_pk)
    if not forum.can_read(request.user):
        raise PermissionDenied
    if request.method == "POST":

        # If the client is using the "preview" button

        if "preview" in request.POST:
            if request.is_ajax():
                content = render_to_response('misc/previsualization.part.html', {'text': request.POST['text']})
                return StreamingHttpResponse(content)
            else:
                form = TopicForm(initial={"title": request.POST["title"],
                                          "subtitle": request.POST["subtitle"],
                                          "text": request.POST["text"]})

                return render(request, "forum/topic/new.html",
                                       {"forum": forum,
                                        "form": form,
                                        "text": request.POST["text"]})
        form = TopicForm(request.POST)
        data = form.data
        if form.is_valid():

            # Treat title

            (tags, title) = get_tag_by_title(data["title"])

            # Creating the thread
            n_topic = Topic()
            n_topic.forum = forum
            n_topic.title = title
            n_topic.subtitle = data["subtitle"]
            n_topic.pubdate = datetime.now()
            n_topic.author = request.user
            n_topic.save()
            # add tags

            n_topic.add_tags(tags)
            n_topic.save()
            # Adding the first message

            post = Post()
            post.topic = n_topic
            post.author = request.user
            post.update_content(request.POST["text"])
            post.pubdate = datetime.now()
            post.position = 1
            post.ip_address = get_client_ip(request)
            post.save()
            n_topic.last_message = post
            n_topic.save()

            # Follow the topic

            follow(n_topic)
            return redirect(n_topic.get_absolute_url())
    else:
        form = TopicForm()

    return render(request, "forum/topic/new.html", {"forum": forum, "form": form})


@can_write_and_read_now
@login_required
@require_POST
@transaction.atomic
def solve_alert(request):
    """
    Solves an alert (i.e. delete it from alert list) and sends an email to the user that created the alert, if the
    resolver leaves a comment.
    This can only be done by staff.
    """

    if not request.user.has_perm("forum.change_post"):
        raise PermissionDenied

    alert = get_object_or_404(Alert, pk=request.POST["alert_pk"])
    post = Post.objects.get(pk=alert.comment.id)

    if "text" in request.POST and request.POST["text"] != "":
        bot = get_object_or_404(User, username=settings.ZDS_APP['member']['bot_account'])
        msg = \
            (u'Bonjour {0},'
             u'Vous recevez ce message car vous avez signalé le message de *{1}*, '
             u'dans le sujet [{2}]({3}). Votre alerte a été traitée par **{4}** '
             u'et il vous a laissé le message suivant :'
             u'\n\n> {5}\n\nToute l\'équipe de la modération vous remercie !'.format(
                 alert.author.username,
                 post.author.username,
                 post.topic.title,
                 settings.ZDS_APP['site']['url'] + post.get_absolute_url(),
                 request.user.username,
                 request.POST["text"],))
        send_mp(
            bot,
            [alert.author],
            u"Résolution d'alerte : {0}".format(post.topic.title),
            "",
            msg,
            False,
        )

    alert.delete()
    messages.success(request, u"L'alerte a bien été résolue.")
    return redirect(post.get_absolute_url())


@can_write_and_read_now
@login_required
@require_POST
@transaction.atomic
def move_topic(request):
    """
    Moves a topic to another forum.
    This can only be done by staff.
    """

    if not request.user.has_perm("forum.change_topic"):
        raise PermissionDenied
    try:
        topic_pk = int(request.GET["sujet"])
    except (KeyError, ValueError):
        # problem in variable format
        raise Http404
    forum = get_object_or_404(Forum, pk=request.POST["forum"])
    if not forum.can_read(request.user):
        raise PermissionDenied
    topic = get_object_or_404(Topic, pk=topic_pk)
    topic.forum = forum
    topic.save()

    # If the topic is moved in a restricted forum, users that cannot read this topic any more un-follow it.
    # This avoids unreachable notifications.
    followers = TopicFollowed.objects.filter(topic=topic)
    for follower in followers:
        if not forum.can_read(follower.user):
            follower.delete()
    messages.success(request,
                     u"Le sujet {0} a bien été déplacé dans {1}."
                     .format(topic.title,
                             forum.title))
    return redirect(topic.get_absolute_url())


@can_write_and_read_now
@login_required
@require_POST
def edit(request):
    """
    Edit the given topic metadata: follow, follow by email, solved, locked, sticky.
    """

    try:
        topic_pk = request.POST['topic']
    except (KeyError, ValueError):
        # problem in variable format
        raise Http404
    if "page" in request.POST:
        try:
            page = int(request.POST["page"])
        except (KeyError, ValueError):
            # problem in variable format
            raise Http404
    else:
        page = 1

    data = request.POST
    resp = {}
    g_topic = get_object_or_404(Topic, pk=topic_pk)
    if "follow" in data:
        resp["follow"] = follow(g_topic)
    if "email" in data:
        resp["email"] = follow_by_email(g_topic)
    if request.user == g_topic.author \
            or request.user.has_perm("forum.change_topic"):
        if "solved" in data:
            g_topic.is_solved = not g_topic.is_solved
            resp["solved"] = g_topic.is_solved
    if request.user.has_perm("forum.change_topic"):

        if "lock" in data:
            g_topic.is_locked = data["lock"] == "true"

            if g_topic.is_locked:
                success_message = u"Le sujet {0} est désormais verrouillé.".format(g_topic.title)
            else:
                success_message = u"Le sujet {0} est désormais déverrouillé.".format(g_topic.title)

            messages.success(request, success_message)
        if 'sticky' in data:
            if data['sticky'] == 'true':
                g_topic.is_sticky = True
                messages.success(request, _(u'Le sujet « {0} » est désormais épinglé.').format(g_topic.title))
            else:
                g_topic.is_sticky = False
                messages.success(request, _(u'Le sujet « {0} » n\'est désormais plus épinglé.').format(g_topic.title))
        # TODO: Doublon avec "move_topic" ?
        if "move" in data:
            try:
                forum_pk = int(request.POST["move_target"])
            except (KeyError, ValueError):
                # problem in variable format
                raise Http404
            forum = get_object_or_404(Forum, pk=forum_pk)
            g_topic.forum = forum
    g_topic.save()
    if request.is_ajax():
        return HttpResponse(json.dumps(resp), content_type='application/json')
    else:
        if not g_topic.forum.can_read(request.user):
            return redirect(reverse('cats-forums-list'))
        else:
            return redirect(u"{}?page={}".format(g_topic.get_absolute_url(),
                                                 page))


@can_write_and_read_now
@login_required
@transaction.atomic
def answer(request):
    """
    Answers (= add a new post) to an existing topic.
    This method also allows the user to preview its answer.
    If there is at least one new answer since the beginning of post redaction, the system displays the updated topic and
    asks the user if it wants to posts its answers as is or edit it before re-send it. This is done by sending the
    "current" last post ID with in the new form post and check on submit if there is a new last post ID.
    """

    try:
        topic_pk = int(request.GET["sujet"])
    except (KeyError, ValueError):
        # problem in variable format
        raise Http404

    # Retrieve current topic.

    g_topic = get_object_or_404(Topic, pk=topic_pk)
    if not g_topic.forum.can_read(request.user):
        raise PermissionDenied

    # Making sure posting is allowed

    if g_topic.is_locked:
        raise PermissionDenied

    # Check that the user isn't spamming

    if g_topic.antispam(request.user):
        raise PermissionDenied
    last_post_pk = g_topic.last_message.pk

    # Retrieve last posts of the current topic.
    posts = Post.objects.filter(topic=g_topic) \
        .prefetch_related() \
        .order_by("-position")[:settings.ZDS_APP['forum']['posts_per_page']]

    # User would like preview his post or post a new post on the topic.

    if request.method == "POST":
        data = request.POST
        newpost = last_post_pk != int(data["last_post"])

        # Using the "preview button", the "more" button or new post

        if "preview" in data or newpost:
            form = PostForm(g_topic, request.user, initial={"text": data["text"]})
            form.helper.form_action = reverse("zds.forum.views.answer") \
                + "?sujet=" + str(g_topic.pk)
            if request.is_ajax():
                content = render_to_response('misc/previsualization.part.html', {'text': data['text']})
                return StreamingHttpResponse(content)
            else:
                return render(request, "forum/post/new.html", {
                    "text": data["text"],
                    "topic": g_topic,
                    "posts": posts,
                    "last_post_pk": last_post_pk,
                    "newpost": newpost,
                    "form": form,
                })
        else:

            # Saving the message

            form = PostForm(g_topic, request.user, request.POST)
            if form.is_valid():
                data = form.data
                post = Post()
                post.topic = g_topic
                post.author = request.user
                post.text = data["text"]
                post.text_html = emarkdown(data["text"])
                post.pubdate = datetime.now()
                post.position = g_topic.get_post_count() + 1
                post.ip_address = get_client_ip(request)
                post.save()
                g_topic.last_message = post
                g_topic.save()

                # Send mail
                subject = u"{} - {} : {}".format(settings.ZDS_APP['site']['litteral_name'],
                                                 _(u'Forum'),
                                                 g_topic.title)
                from_email = "{} <{}>".format(settings.ZDS_APP['site']['litteral_name'],
                                              settings.ZDS_APP['site']['email_noreply'])

                followers = g_topic.get_followers_by_email()
                for follower in followers:
                    receiver = follower.user
                    if receiver == request.user:
                        continue
                    pos = post.position - 1
                    last_read = TopicRead.objects.filter(
                        topic=g_topic,
                        post__position=pos,
                        user=receiver).count()
                    if last_read > 0:
                        context = {
                            'username': receiver.username,
                            'title': g_topic.title,
                            'url': settings.ZDS_APP['site']['url'] + post.get_absolute_url(),
                            'author': request.user.username,
                            'site_name': settings.ZDS_APP['site']['litteral_name']
                        }
                        message_html = render_to_string('email/forum/new_post.html', context)
                        message_txt = render_to_string('email/forum/new_post.txt', context)

                        msg = EmailMultiAlternatives(subject, message_txt, from_email, [receiver.email])
                        msg.attach_alternative(message_html, "text/html")
                        msg.send()

                # Follow topic on answering
                if not g_topic.is_followed(user=request.user):
                    follow(g_topic)
                return redirect(post.get_absolute_url())
            else:
                return render(request, "forum/post/new.html", {
                    "text": data["text"],
                    "topic": g_topic,
                    "posts": posts,
                    "last_post_pk": last_post_pk,
                    "newpost": newpost,
                    "form": form,
                })
    else:

        # Actions from the editor render to new.html.

        text = ""

        # Using the quote button

        if "cite" in request.GET:
            resp = {}
            try:
                post_cite_pk = int(request.GET["cite"])
            except ValueError:
                raise Http404
            post_cite = Post.objects.get(pk=post_cite_pk)
            if not post_cite.is_visible:
                raise PermissionDenied
            for line in post_cite.text.splitlines():
                text = text + "> " + line + "\n"
            text = u"{0}Source:[{1}]({2}{3})".format(
                text,
                post_cite.author.username,
                settings.ZDS_APP['site']['url'],
                post_cite.get_absolute_url())

            if request.is_ajax():
                resp["text"] = text
                return HttpResponse(json.dumps(resp), content_type='application/json')

        form = PostForm(g_topic, request.user, initial={"text": text})
        form.helper.form_action = reverse("zds.forum.views.answer") \
            + "?sujet=" + str(g_topic.pk)
        return render(request, "forum/post/new.html", {
            "topic": g_topic,
            "posts": posts,
            "last_post_pk": last_post_pk,
            "form": form,
        })


@can_write_and_read_now
@login_required
@transaction.atomic
def edit_post(request):
    """
    Edit a post. Also allows to signal a message to the staff, hide a message (in code this is called "delete" it), show
    it and allows the user to preview its edited message.
    This displays a warning if a moderator edits someone else's post.
    """

    try:
        post_pk = int(request.GET["message"])
    except (KeyError, ValueError):
        # problem in variable format
        raise Http404

    post = get_object_or_404(Post, pk=post_pk)

    # check if the user can use this forum
    if not post.topic.forum.can_read(request.user):
        raise PermissionDenied

    if post.position <= 1:
        g_topic = get_object_or_404(Topic, pk=post.topic.pk)
    else:
        g_topic = None

    # Making sure the user is allowed to do that. Author of the post must to be
    # the user logged.
    if post.author != request.user \
            and not request.user.has_perm("forum.change_post") and "signal_message" \
            not in request.POST:
        raise PermissionDenied
    if post.author != request.user and request.method == "GET" \
            and request.user.has_perm("forum.change_post"):
        messages.warning(request,
                         _(u'Vous éditez ce message en tant que '
                           u'modérateur (auteur : {}). Soyez encore plus '
                           u'prudent lors de l\'édition de celui-ci !')
                         .format(post.author.username))

    if request.method == "POST":
        if "delete_message" in request.POST:
            if post.author == request.user \
                    or request.user.has_perm("forum.change_post"):
                post.alerts.all().delete()
                post.is_visible = False

                if request.user.has_perm("forum.change_post"):
                    post.text_hidden = request.POST["text_hidden"]

                post.editor = request.user

                messages.success(request, _(u"Le message est désormais masqué."))
        elif "show_message" in request.POST:
            if request.user.has_perm("forum.change_post"):
                post.is_visible = True
                post.text_hidden = ""
        elif "signal_message" in request.POST:
            alert = Alert()
            alert.author = request.user
            alert.comment = post
            alert.scope = Alert.FORUM
            alert.text = request.POST['signal_text']
            alert.pubdate = datetime.now()
            alert.save()

            messages.success(request,
                             _(u'Une alerte a été envoyée '
                               u'à l\'équipe concernant '
                               u'ce message.'))
        elif "preview" in request.POST:
            if request.is_ajax():
                content = render_to_response('misc/previsualization.part.html', {'text': request.POST['text']})
                return StreamingHttpResponse(content)
            else:
                if g_topic:
                    form = TopicForm(initial={"title": request.POST["title"],
                                              "subtitle": request.POST["subtitle"],
                                              "text": request.POST["text"]})
                else:
                    form = PostForm(post.topic, request.user,
                                    initial={"text": request.POST["text"]})

                form.helper.form_action = reverse("zds.forum.views.edit_post") \
                    + "?message=" + str(post_pk)

                return render(request, "forum/post/edit.html", {
                    "post": post,
                    "topic": post.topic,
                    "text": request.POST["text"],
                    "form": form,
                })
        else:
            # The user just sent data, handle them
            if "text" in request.POST:
                form = TopicForm(request.POST)
                form_post = PostForm(post.topic, request.user, request.POST)

                if not form.is_valid():
                    if g_topic:
                        return render(request, "forum/post/edit.html", {
                            "post": post,
                            "topic": post.topic,
                            "text": post.text,
                            "form": form,
                        })
                    elif not form_post.is_valid():
                        form = PostForm(post.topic, request.user, initial={"text": post.text})
                        form._errors = form_post._errors
                        return render(request, "forum/post/edit.html", {
                            "post": post,
                            "topic": post.topic,
                            "text": post.text,
                            "form": form,
                        })

                post.text = request.POST["text"]
                post.text_html = emarkdown(request.POST["text"])
                post.update = datetime.now()
                post.editor = request.user

            # Modifying the thread info
            if g_topic:
                (tags, title) = get_tag_by_title(request.POST["title"])
                g_topic.title = title
                g_topic.subtitle = request.POST["subtitle"]
                g_topic.save()

                # add tags
                g_topic.tags.clear()
                g_topic.add_tags(tags)

        post.save()
        return redirect(post.get_absolute_url())
    else:
        if g_topic:
            prefix = u""
            for tag in g_topic.tags.all():
                prefix += u"[{0}]".format(tag.title)

            form = TopicForm(
                initial={
                    "title": u"{0} {1}".format(
                        prefix,
                        g_topic.title).strip(),
                    "subtitle": g_topic.subtitle,
                    "text": post.text})
        else:
            form = PostForm(post.topic, request.user,
                            initial={"text": post.text})

        form.helper.form_action = reverse("zds.forum.views.edit_post") \
            + "?message=" + str(post_pk)
        return render(request, "forum/post/edit.html", {
            "post": post,
            "topic": post.topic,
            "text": post.text,
            "form": form,
        })


@can_write_and_read_now
@login_required
@require_POST
def useful_post(request):
    """
    Marks an answer as "useful".
    Only the topic creator (or a staff member) can do this.
    """

    try:
        post_pk = int(request.GET["message"])
    except (KeyError, ValueError):
        # problem in variable format
        raise Http404
    post = get_object_or_404(Post, pk=post_pk)

    # check that author can access the forum

    if not post.topic.forum.can_read(request.user):
        raise PermissionDenied

    # Making sure the user is allowed to do that

    if post.author == request.user or request.user != post.topic.author:
        if not request.user.has_perm("forum.change_post"):
            raise PermissionDenied
    post.is_useful = not post.is_useful
    post.save()

    if request.is_ajax():
        return HttpResponse(json.dumps(post.is_useful), content_type='application/json')

    return redirect(post.get_absolute_url())


@can_write_and_read_now
@login_required
def unread_post(request):
    """
    Marks a post as "unread".
    If the post is the first one, the whole topic is marked as "unread" - technically, the whole `TopicRead` entry is
    removed.
    If the post is not the first one, the topic is marked as "unread" starting this post - technically, the `TopicRead`
    entry is updated or created.
    """

    try:
        post_pk = int(request.GET["message"])
    except (KeyError, ValueError):
        # problem in variable format
        raise Http404
    post = get_object_or_404(Post, pk=post_pk)

    # check that author can access the forum

    if not post.topic.forum.can_read(request.user):
        raise PermissionDenied
    if TopicFollowed.objects.filter(user=request.user, topic=post.topic).count() == 0:
        TopicFollowed(user=request.user, topic=post.topic).save()

    t = TopicRead.objects.filter(topic=post.topic, user=request.user).first()
    if t is None:
        if post.position > 1:
            unread = Post.objects.filter(topic=post.topic, position=(post.position - 1)).first()
            t = TopicRead(post=unread, topic=unread.topic, user=request.user)
            t.save()
    else:
        if post.position > 1:
            unread = Post.objects.filter(topic=post.topic, position=(post.position - 1)).first()
            t.post = unread
            t.save()
        else:
            t.delete()

    return redirect(reverse("forum-topics-list", args=[post.topic.forum.category.slug, post.topic.forum.slug]))


# TODO nécessite une transaction ?
@can_write_and_read_now
@login_required
@require_POST
def like_post(request):
    """
    The current user likes the post, or stops to like it if it was already liked. A like replaces the eventual existing
    dislike on this post.
    As the "like/dislike" data is de-normalized for performance purposes, also this also updates the de-normalized data
    at `Post` level.
    """

    try:
        post_pk = int(request.GET["message"])
    except (KeyError, ValueError):
        # problem in variable format
        raise Http404
    resp = {}
    post = get_object_or_404(Post, pk=post_pk)
    user = request.user
    if not post.topic.forum.can_read(request.user):
        raise PermissionDenied
    if post.author.pk != request.user.pk:

        # Making sure the user is allowed to do that

        if CommentLike.objects.filter(user__pk=user.pk,
                                      comments__pk=post_pk).count() == 0:
            like = CommentLike()
            like.user = user
            like.comments = post
            post.like = post.like + 1
            post.save()
            like.save()
            if CommentDislike.objects.filter(user__pk=user.pk,
                                             comments__pk=post_pk).count() > 0:
                CommentDislike.objects.filter(
                    user__pk=user.pk,
                    comments__pk=post_pk).all().delete()
                post.dislike = post.dislike - 1
                post.save()
        else:
            CommentLike.objects.filter(user__pk=user.pk,
                                       comments__pk=post_pk).all().delete()
            post.like = post.like - 1
            post.save()
    resp["upvotes"] = post.like
    resp["downvotes"] = post.dislike
    if request.is_ajax():
        return HttpResponse(json.dumps(resp), content_type='application/json')
    else:
        return redirect(post.get_absolute_url())


# TODO nécessite une transaction ?
@can_write_and_read_now
@login_required
@require_POST
def dislike_post(request):
    """
    The current user dislikes the post, or stops to dislike it if it was already liked. A dislike replaces the eventual
    existing like on this post.
    As the "like/dislike" data is de-normalized for performance purposes, also this also updates the de-normalized data
    at `Post` level.
    """

    try:
        post_pk = int(request.GET["message"])
    except (KeyError, ValueError):
        # problem in variable format
        raise Http404
    resp = {}
    post = get_object_or_404(Post, pk=post_pk)
    user = request.user
    if not post.topic.forum.can_read(request.user):
        raise PermissionDenied
    if post.author.pk != request.user.pk:

        # Making sure the user is allowed to do that

        if CommentDislike.objects.filter(user__pk=user.pk,
                                         comments__pk=post_pk).count() == 0:
            dislike = CommentDislike()
            dislike.user = user
            dislike.comments = post
            post.dislike = post.dislike + 1
            post.save()
            dislike.save()
            if CommentLike.objects.filter(user__pk=user.pk,
                                          comments__pk=post_pk).count() > 0:
                CommentLike.objects.filter(user__pk=user.pk,
                                           comments__pk=post_pk).all().delete()
                post.like = post.like - 1
                post.save()
        else:
            CommentDislike.objects.filter(user__pk=user.pk,
                                          comments__pk=post_pk).all().delete()
            post.dislike = post.dislike - 1
            post.save()
    resp["upvotes"] = post.like
    resp["downvotes"] = post.dislike
    if request.is_ajax():
        return HttpResponse(json.dumps(resp))
    else:
        return redirect(post.get_absolute_url())


# TODO: PK AND slug -> one of them is probably useless
def find_topic_by_tag(request, tag_pk, tag_slug):
    """
    Displays all topics with provided tag. Also handles a filter on "solved". Only the topics the user can read are
    displayed.
    The tag primary key and the tag slug must match, otherwise nothing will be found.
    Topics found are displayed paginated with `settings.ZDS_APP['forum']['topics_per_page']` topics per page.
    :param tag_pk: the primary key of the tag to find by
    :param tag_slug: the slug of the tag to find by
    """

    tag = Tag.objects.filter(pk=tag_pk, slug=tag_slug).first()
    if tag is None:
        return redirect(reverse('cats-forums-list'))
    u = request.user
    if "filter" in request.GET:
        filter = request.GET["filter"]
        if request.GET["filter"] == "solve":
            topics = Topic.objects.filter(
                tags__in=[tag],
                is_solved=True).order_by("-last_message__pubdate").prefetch_related(
                    "author",
                    "last_message",
                    "tags")\
                .exclude(Q(forum__group__isnull=False) & ~Q(forum__group__in=u.groups.all()))\
                .all()
        else:
            topics = Topic.objects.filter(
                tags__in=[tag],
                is_solved=False).order_by("-last_message__pubdate")\
                .prefetch_related(
                    "author",
                    "last_message",
                    "tags")\
                .exclude(Q(forum__group__isnull=False) & ~Q(forum__group__in=u.groups.all()))\
                .all()
    else:
        filter = None
        topics = Topic.objects.filter(tags__in=[tag]).order_by("-last_message__pubdate")\
            .exclude(Q(forum__group__isnull=False) & ~Q(forum__group__in=u.groups.all()))\
            .prefetch_related("author", "last_message", "tags").all()
    # Paginator

    paginator = Paginator(topics, settings.ZDS_APP['forum']['topics_per_page'])
    page = request.GET.get("page")
    try:
        shown_topics = paginator.page(page)
        page = int(page)
    except PageNotAnInteger:
        shown_topics = paginator.page(1)
        page = 1
    except EmptyPage:
        raise Http404
    return render(request, "forum/find/topic_by_tag.html", {
        "topics": shown_topics,
        "tag": tag,
        "pages": paginator_range(page, paginator.num_pages),
        "nb": page,
        "filter": filter,
    })


def find_topic(request, user_pk):
    """
    Displays all topics created by the provided user, paginated with `settings.ZDS_APP['forum']['topics_per_page']`
    topics per page.
    Only the topics the current user can read are displayed.
    :param user_pk: the user to find the topics.
    """

    displayed_user = get_object_or_404(User, pk=user_pk)
    topics = \
        Topic.objects\
        .filter(author=displayed_user)\
        .exclude(Q(forum__group__isnull=False) & ~Q(forum__group__in=request.user.groups.all()))\
        .prefetch_related("author")\
        .order_by("-pubdate").all()

    # Paginator
    paginator = Paginator(topics, settings.ZDS_APP['forum']['topics_per_page'])
    page = request.GET.get("page")
    try:
        shown_topics = paginator.page(page)
        page = int(page)
    except PageNotAnInteger:
        shown_topics = paginator.page(1)
        page = 1
    except EmptyPage:
        shown_topics = paginator.page(paginator.num_pages)
        page = paginator.num_pages

    return render(request, "forum/find/topic.html", {
        "topics": shown_topics,
        "usr": displayed_user,
        "pages": paginator_range(page, paginator.num_pages),
        "nb": page,
    })


def find_post(request, user_pk):
    """
    Displays all posts of a user, paginated with `settings.ZDS_APP['forum']['posts_per_page']` post per page.
    Only the posts the current user can read are displayed.
    :param user_pk: the user to find the posts.
    """

    displayed_user = get_object_or_404(User, pk=user_pk)
    user = request.user

    if user.has_perm("forum.change_post"):
        posts = \
            Post.objects.filter(author=displayed_user)\
            .exclude(Q(topic__forum__group__isnull=False) & ~Q(topic__forum__group__in=user.groups.all()))\
            .prefetch_related("author")\
            .order_by("-pubdate").all()
    else:
        posts = \
            Post.objects.filter(author=displayed_user)\
            .filter(is_visible=True)\
            .exclude(Q(topic__forum__group__isnull=False) & ~Q(topic__forum__group__in=user.groups.all()))\
            .prefetch_related("author").order_by("-pubdate").all()

    # Paginator
    paginator = Paginator(posts, settings.ZDS_APP['forum']['posts_per_page'])
    page = request.GET.get("page")
    try:
        shown_posts = paginator.page(page)
        page = int(page)
    except PageNotAnInteger:
        shown_posts = paginator.page(1)
        page = 1
    except EmptyPage:
        shown_posts = paginator.page(paginator.num_pages)
        page = paginator.num_pages

    return render(request, "forum/find/post.html", {
        "posts": shown_posts,
        "usr": displayed_user,
        "pages": paginator_range(page, paginator.num_pages),
        "nb": page,
    })


@login_required
def followed_topics(request):
    """
    Displays the followed topics fo the current user, with `settings.ZDS_APP['forum']['followed_topics_per_page']`
    topics per page.
    """
    followed_topics = request.user.profile.get_followed_topics()

    # Paginator

    paginator = Paginator(followed_topics, settings.ZDS_APP['forum']['followed_topics_per_page'])
    page = request.GET.get("page")
    try:
        shown_topics = paginator.page(page)
        page = int(page)
    except PageNotAnInteger:
        shown_topics = paginator.page(1)
        page = 1
    except EmptyPage:
        shown_topics = paginator.page(paginator.num_pages)
        page = paginator.num_pages
    return render(request, "forum/topic/followed.html",
                           {"followed_topics": shown_topics,
                            "pages": paginator_range(page,
                                                     paginator.num_pages),
                            "nb": page})


# TODO suggestions de recherche auto lors d'un nouveau topic, cf issues #99 et #580. Actuellement désactivées :(
def complete_topic(request):
    if not request.GET.get('q', None):
        return HttpResponse("{}", content_type='application/json')

    # TODO: WTF "sqs" ?!
    sqs = SearchQuerySet().filter(content=AutoQuery(request.GET.get('q'))).order_by('-pubdate').all()

    suggestions = {}

    cpt = 0
    for result in sqs:
        if cpt > 5:
            break
        if 'Topic' in str(result.model) and result.object.is_solved:
            suggestions[str(result.object.pk)] = (result.title, result.author, result.object.get_absolute_url())
            cpt += 1

    the_data = json.dumps(suggestions)

    return HttpResponse(the_data, content_type='application/json')
