from django.shortcuts import render_to_response
from django.template import RequestContext
from django.template.loader import render_to_string
from django.http import HttpResponseRedirect, HttpResponse
from life.models import Locale, Push, Changeset, Tree
from shipping.models import Milestone, Signoff, Snapshot, AppVersion, Action, SignoffForm, ActionForm
from l10nstats.models import Run
from django import forms
from django.conf import settings
from django.core.urlresolvers import reverse
from django.utils import simplejson
from django.core import serializers
from django.db import connection
from django.db.models import Max

from collections import defaultdict
from ConfigParser import ConfigParser
import datetime
from difflib import SequenceMatcher
import re

from Mozilla.Parser import getParser, Junk
from Mozilla.CompareLocales import AddRemove, Tree as DataTree


def index(request):
    locales = Locale.objects.all().order_by('code')
    avs = AppVersion.objects.all().order_by('code')

    for i in avs:
        statuses = Milestone.objects.filter(appver=i.id).values_list('status', flat=True).distinct()
        if 1 in statuses:
            i.status = 'open'
        elif 0 in statuses:
            i.status = 'upcoming'
        elif 2 in statuses:
            i.status = 'shipped'
        else:
            i.status = 'unknown' 

    return render_to_response('shipping/index.html', {
        'locales': locales,
        'avs': avs,
    })

def homesnippet(request):
    miles = Milestone.objects.filter(status=1).order_by('code')
    return render_to_string('shipping/snippet.html', {
            'miles': miles,
            })

def pushes(request):
    if request.GET.has_key('locale'):
        locale = Locale.objects.get(code=request.GET['locale'])
    if request.GET.has_key('ms'):
        mstone = Milestone.objects.get(code=request.GET['ms'])
        appver = mstone.appver
    if request.GET.has_key('av'):
        appver = AppVersion.objects.get(code=request.GET['av'])
        try:
            mstone = Milestone.objects.filter(appver__code=request.GET['av']).order_by('-pk')[0]
        except:
            mstone = None
    enabled = mstone is None or mstone.status<2
    if enabled:
        current = _get_current_signoff(locale, ms=mstone, av=appver)
    else:
        current = _get_accepted_signoff(locale, ms=mstone, av=appver)
    user = request.user
    anonymous = user.is_anonymous()
    staff = user.is_staff
    if request.method == 'POST': # we're going to process forms
        offset_id = request.POST['first_row']
        if not enabled: # ... but we're not logged in. Panic!
            request.session['signoff_error'] = '<span style="font-style: italic">Signoff for %s %s</span> could not be added - <strong>Milestone is not open for edits</strong>' % (mstone, locale)
        elif anonymous: # ... but we're not logged in. Panic!
            request.session['signoff_error'] = '<span style="font-style: italic">Signoff for %s %s</span> could not be added - <strong>User not logged in</strong>' % (appver, locale)
        else:
            if request.POST.has_key('accepted'): # we're in AcceptedForm mode
                if not staff: # ... but we have no privileges for that!
                    request.session['signoff_error'] = '<span style="font-style: italic">Signoff for %s %s</span> could not be accepted/rejected - <strong>User has not enough privileges</strong>' % (mstone or appver, locale)
                else:
                    # hack around AcceptForm not taking strings, fixed in
                    # django 1.1
                    bval = {"true": 1, "false": 2}[request.POST['accepted']]
                    form = ActionForm({'signoff': current.id, 'flag': bval, 'author': user.id, 'comment': request.POST['comment']})
                    if form.is_valid():
                        form.save()
                        if request.POST['accepted'] == "false":
                            request.session['signoff_info'] = '<span style="font-style: italic">Rejected'
                        else:
                            request.session['signoff_info'] = '<span style="font-style: italic">Accepted'
                    else:
                        request.session['signoff_error'] = '<span style="font-style: italic">Signoff for %s %s by %s</span> could not be added' % (mstone or appver, locale, user.username)
            else:
                instance = Signoff(appversion=appver, locale=locale, author=user)
                form = SignoffForm(request.POST, instance=instance)
                if form.is_valid():
                    form.save()
                    
                    #add a snapshot of the current test results
                    pushobj = Push.objects.get(id=request.POST['push'])
                    lastrun = _get_compare_locales_result(pushobj.tip, appver.tree)
                    if lastrun:
                        Snapshot.objects.create(signoff_id=form.instance.id, test=Run, tid=lastrun.id)
                    Action.objects.create(signoff_id=form.instance.id, flag=0, author=user)

                    request.session['signoff_info'] = '<span style="font-style: italic">Signoff for %s %s by %s</span> added' % (mstone or appver, locale, user.username)
                else:
                    request.session['signoff_error'] = '<span style="font-style: italic">Signoff for %s %s by %s</span> could not be added' % (mstone or appver, locale, user.username)
        if request.GET.has_key('av'):
            return HttpResponseRedirect('%s?locale=%s&av=%s&offset=%s' % (reverse('signoff.views.pushes'), locale.code ,appver.code, offset_id))
        else:
            return HttpResponseRedirect('%s?locale=%s&ms=%s&offset=%s' % (reverse('signoff.views.pushes'), locale.code ,mstone.code, offset_id))

    form = SignoffForm()
    
    forest = appver.tree.l10n
    repo_url = '%s%s/' % (forest.url, locale.code)
    notes = _get_notes(request.session)
    accepted = _get_accepted_signoff(locale, ms=mstone, av=appver)

    max_pushes = _get_total_pushes(locale, mstone)
    if max_pushes > 50:
        max_pushes = 50

    if request.GET.has_key('center'):
        offset = _get_push_offset(request.GET['center'],-5)
    elif request.GET.has_key('offset'):
        offset = _get_push_offset(request.GET['offset'])
    else:
        offset = 0
    return render_to_response('shipping/pushes.html', {
        'mstone': mstone,
        'appver': appver,
        'locale': locale,
        'form': form,
        'notes': notes,
        'current': current,
        'accepted': accepted,
        'user': user,
        'user_type': 0 if user.is_anonymous() else 2 if user.is_staff else 1,
        'pushes': (simplejson.dumps(_get_api_items(locale, appver, current, offset=offset+20)), 0, min(max_pushes,offset+10)),
        'max_pushes': max_pushes,
        'offset': offset,
        'current_js': simplejson.dumps(_get_current_js(current)),
    })


def diff_app(request):
    reponame = request.GET['repo']
    repopath = settings.REPOSITORY_BASE + '/' + reponame
    from mercurial.ui import ui as _ui
    from mercurial.hg import repository
    ui = _ui()
    repo = repository(ui, repopath)
    ctx1 = repo.changectx(request.GET['from'])
    ctx2 = repo.changectx(request.GET['to'])
    match = None # maybe get something from l10n.ini and cmdutil
    changed, added, removed = repo.status(ctx1, ctx2, match=match)[:3]
    diffs = DataTree(dict)
    for path in changed:
        lines = []
        try:
            p = getParser(path)
        except UserWarning:
            diffs[path].update({'path': path,
                                'lines': [{'class': 'issue',
                                           'oldval': '',
                                           'newval': '',
                                           'entity': 'cannot parse ' + path}]})
            continue
        data1 = ctx1.filectx(path).data()
        data2 = ctx2.filectx(path).data()
        p.readContents(data1)
        a_entities, a_map = p.parse()
        p.readContents(data2)
        c_entities, c_map = p.parse()
        del p
        a_list = sorted(a_map.keys())
        c_list = sorted(c_map.keys())
        ar = AddRemove()
        ar.set_left(a_list)
        ar.set_right(c_list)
        for action, item_or_pair in ar:
            if action == 'delete':
                lines.append({'class': 'removed',
                              'oldval': [{'value':a_entities[a_map[item_or_pair]].val}],
                              'newval': '',
                              'entity': item_or_pair})
            elif action == 'add':
                lines.append({'class': 'added',
                              'oldval': '',
                              'newval':[{'value': c_entities[c_map[item_or_pair]].val}],
                              'entity': item_or_pair})
            else:
                oldval = a_entities[a_map[item_or_pair[0]]].val
                newval = c_entities[c_map[item_or_pair[1]]].val
                if oldval == newval:
                    continue
                sm = SequenceMatcher(None, oldval, newval)
                oldhtml = []
                newhtml = []
                for op, o1, o2, n1, n2 in sm.get_opcodes():
                    if o1 != o2:
                        oldhtml.append({'class':op, 'value':oldval[o1:o2]})
                    if n1 != n2:
                        newhtml.append({'class':op, 'value':newval[n1:n2]})
                lines.append({'class':'changed',
                              'oldval': oldhtml,
                              'newval': newhtml,
                              'entity': item_or_pair[0]})
        container_class = lines and 'file' or 'empty-diff'
        diffs[path].update({'path': path,
                            'class': container_class,
                            'lines': lines})
    diffs = diffs.toJSON().get('children', [])
    return render_to_response('shipping/diff.html',
                              {'locale': request.GET['locale'],
                               'added': added,
                               'removed': removed,
                               'repo_url': request.GET['url'],
                               'old_rev': request.GET['from'],
                               'new_rev': request.GET['to'],
                               'diffs': diffs})


def dashboard(request):
    if 'ms' in request.GET:
        mstone = Milestone.objects.get(code=request.GET['ms'])
        tree = mstone.appver.tree
        obj = mstone
        query = 'ms'
    else:
        appver = AppVersion.objects.get(code=request.GET['av'])
        tree = appver.tree
        obj = appver
        query = 'av'
    args = ["tree=%s" % tree.code]
    return render_to_response('shipping/dashboard.html', {
            'obj': obj,
            'query': query,
            'args': args,
            })

def l10n_changesets(request):
    if request.GET.has_key('ms'):
        av_or_m = Milestone.objects.get(code=request.GET['ms'])
    elif request.GET.has_key('av'):
        av_or_m = AppVersion.objects.get(code=request.GET['av'])
    else:
        return HttpResponse('No milestone or appversion given')

    sos = _signoffs(av_or_m).annotate(tip=Max('push__changesets__id'))
    tips = dict(sos.values_list('locale__code', 'tip'))
    revmap = dict(Changeset.objects.filter(id__in=tips.values()).values_list('id', 'revision'))
    r = HttpResponse(('%s %s\n' % (l, revmap[tips[l]][:12])
                      for l in sorted(tips.keys())),
                     content_type='text/plain; charset=utf-8')
    r['Content-Disposition'] = 'inline; filename=l10n-changesets'
    return r

def shipped_locales(request):
    if request.GET.has_key('ms'):
        av_or_m = Milestone.objects.get(code=request.GET['ms'])
    elif request.GET.has_key('av'):
        av_or_m = AppVersion.objects.get(code=request.GET['av'])
    else:
        return HttpResponse('No milestone or appversion given')

    sos = _signoffs(av_or_m).values_list('locale__code', flat=True)
    locales = list(sos) + ['en-US']
    def withPlatforms(loc):
        if loc == 'ja':
            return 'ja linux win32\n'
        if loc == 'ja-JP-mac':
            return 'ja-JP-mac osx\n'
        return loc + '\n'
    
    r = HttpResponse(map(withPlatforms, sorted(locales)),
                      content_type='text/plain; charset=utf-8')
    r['Content-Disposition'] = 'inline; filename=shipped-locales'
    return r

def signoff_json(request):
    if request.GET.has_key('ms'):
        av_or_m = Milestone.objects.get(code=request.GET['ms'])
        appvers = AppVersion.object.filter(app=av_or_m.appver.app)
    elif request.GET.has_key('av'):
        av_or_m = AppVersion.objects.get(code=request.GET['av'])
        appvers = AppVersion.objects.filter(app=av_or_m.app)
    lsd = _signoffs(av_or_m, getlist=True)
    items = defaultdict(list)
    values = dict(Action._meta.get_field('flag').flatchoices)
    for loc, sol in lsd.iteritems():
        items[loc] = [values[so] for so in sol]
    # get shipped-in data, latest milestone of all appversions for now
    shipped_in = defaultdict(list)
    for _av in appvers:
        try:
            _ms = _av.milestone_set.filter(status=2).order_by('-pk')[0]
        except IndexError:
            continue
        for loc in _ms.signoffs.values_list('locale__code', flat=True):
            shipped_in[loc].append(_ms.code)
    # make a list now
    items = [{"type": "SignOff", "label": locale, 'signoff': list(values)}
             for locale, values in items.iteritems()]
    items += [{"type": "Shippings", "label": locale, 'shipped': stones}
              for locale, stones in shipped_in.iteritems()]
    return HttpResponse(simplejson.dumps({'items': items}, indent=2))


def pushes_json(request):
    loc = request.GET.get('locale', None)
    ms = request.GET.get('mstone', None)
    appver = request.GET.get('av', None)
    start = int(request.GET.get('from', 0))
    to = int(request.GET.get('to', 20))
    
    locale = None
    mstone = None
    cur = None
    if loc:
        locale = Locale.objects.get(code=loc)
    if ms:
        mstone = Milestone.objects.get(code=ms)
        appver = mstone.appver
    elif appver:
        appver = AppVersion.objects.get(code=appver)
    if loc and ms:
        cur = _get_current_signoff(locale, mstone)
    
    pushes = _get_api_items(locale, appver, cur, start=start, offset=start+to)
    return HttpResponse(simplejson.dumps({'items': pushes}, indent=2))


def milestones(request):
    """Administrate milestones.

    Opens an exhibit that offers the actions below depending on 
    milestone status and user permissions.
    """
    return render_to_response('shipping/milestones.html',
                              {},
                              context_instance=RequestContext(request))

def stones_data(request):
    """JSON data to be used by milestones
    """
    stones = Milestone.objects.order_by('-pk').select_related(depth=1)[:5]
    items = [{'label': str(stone),
              'appver': str(stone.appver),
              'status': stone.status,
              'code': stone.code}
             for stone in stones]
    return HttpResponse(simplejson.dumps({'items': items}, indent=2))

def open_mstone(request):
    """Open a milestone.

    Only available to POST, and requires signoff.can_open permissions.
    Redirects to milestones().
    """
    if (request.method == "POST" and
        'ms' in request.POST and
        request.user.has_perm('shipping.can_open')):
        try:
            mstone = Milestone.objects.get(code=request.POST['ms'])
            mstone.status = 1
            # XXX create event
            mstone.save()
        except:
            pass
    return HttpResponseRedirect(reverse('shipping.views.milestones'))

def clear_mstone(request):
    """Clear a milestone, reset all sign-offs.

    Only available to POST, and requires signoff.can_open permissions.
    Redirects to dasboard() for the milestone.
    """
    if (request.method == "POST" and
        'ms' in request.POST and
        request.user.has_perm('shipping.can_open')):
        try:
            mstone = Milestone.objects.get(code=request.POST['ms'])
            if mstone.status is 2:
                return HttpResponseRedirect(reverse('shipping.views.milestones'))
            # get all signoffs, independent of state, and file an obsolete
            # action
            for so in _signoffs(mstone, status=None):
                so.action_set.create(flag=4, author=request.user)
            return HttpResponseRedirect(reverse('shipping.views.dashboard')
                                        + "?ms=" + mstone.code)
        except:
            pass
    return HttpResponseRedirect(reverse('shipping.views.milestones'))


def _propose_mstone(mstone):
    """Propose a new milestone based on an existing one.

    Tries to find the last integer in name and version, increment that
    and create a new milestone.
    """
    last_int = re.compile('(\d+)$')
    name_m = last_int.search(mstone.name)
    if name_m is None:
        return None
    code_m = last_int.search(mstone.code)
    if code_m is None:
        return None
    name_int = int(name_m.group())
    code_int = int(code_m.group())
    if name_int != code_int:
        return None
    new_rev = str(name_int + 1)
    return dict(code=last_int.sub(new_rev, mstone.code),
                name=last_int.sub(new_rev, mstone.name),
                appver=mstone.appver.code)


def confirm_ship_mstone(request):
    """Intermediate page when shipping a milestone.

    Gathers all data to verify when shipping.
    Ends up in ship_mstone if everything is fine.
    Redirects to milestones() in case of trouble.
    """
    if not ("ms" in request.GET and
            request.user.has_perm('shipping.can_ship')):
        return HttpResponseRedirect(reverse('shipping.views.milestones'))
    try:
        mstone = Milestone.objects.get(code=request.GET['ms'])
    except:
        return HttpResponseRedirect(reverse('shipping.views.milestones'))
    if mstone.status != 1:
        return HttpResponseRedirect(reverse('shipping.views.milestones'))
    statuses = _signoffs(mstone, getlist=True)
    pending_locs = []
    good = 0
    for loc, flags in statuses.iteritems():
        if 0 in flags:
            # pending
            pending_locs.append(loc)
        if 1 in flags:
            # good
            good += 1
    pending_locs.sort()
    return render_to_response('shipping/confirm-ship.html',
                              {'mstone': mstone,
                               'pending_locs': pending_locs,
                               'good': good},
                              context_instance=RequestContext(request))
        
def ship_mstone(request):
    """The actual worker method to ship a milestone.

    Only avaible to POST.
    Redirects to milestones().
    """
    if (request.method == "POST" and
        'ms' in request.POST and
        request.user.has_perm('shipping.can_ship')):
        try:
            mstone = Milestone.objects.get(code=request.POST['ms'])
            # get current signoffs
            cs = _signoffs(mstone).values_list('id', flat=True)
            mstone.signoffs.add(*list(cs))  # add them
            mstone.status = 2
            # XXX create event
            mstone.save()
        except:
            pass
    return HttpResponseRedirect(reverse('shipping.views.milestones'))


def confirm_drill_mstone(request):
    """Intermediate page when fire-drilling a milestone.

    Gathers all data to verify when shipping.
    Ends up in drill_mstone if everything is fine.
    Redirects to milestones() in case of trouble.
    """
    if not ("ms" in request.GET and
            request.user.has_perm('shipping.can_ship')):
        return HttpResponseRedirect(reverse('shipping.views.milestones'))
    try:
        mstone = Milestone.objects.get(code=request.GET['ms'])
    except:
        return HttpResponseRedirect(reverse('shipping.views.milestones'))
    if mstone.status != 1:
        return HttpResponseRedirect(reverse('shipping.views.milestones'))

    drill_base = Milestone.objects.filter(appver=mstone.appver,status=2).order_by('-pk').select_related()
    proposed = _propose_mstone(mstone)

    return render_to_response('shipping/confirm-drill.html',
                              {'mstone': mstone,
                               'older': drill_base[:3],
                               'proposed': proposed,
                               },
                              context_instance=RequestContext(request))

def drill_mstone(request):
    """The actual worker method to ship a milestone.

    Only avaible to POST.
    Redirects to milestones().
    """
    if (request.method == "POST" and
        'ms' in request.POST and
        'base' in request.POST and
        request.user.has_perm('shipping.can_ship')):
        try:
            import pdb
            pdb.set_trace()
            mstone = Milestone.objects.get(code=request.POST['ms'])
            base = Milestone.objects.get(code=request.POST['base'])
            so_ids = list(base.signoffs.values_list('id', flat=True))
            mstone.signoffs = so_ids  # add signoffs of base ms
            mstone.status = 2
            # XXX create event
            mstone.save()
        except Exception, e:
            pass
    return HttpResponseRedirect(reverse('shipping.views.milestones'))


#
#  Internal functions
#

def _get_current_signoff(locale, ms=None, av=None):
    if av:
        sos = Signoff.objects.filter(locale=locale, appversion=av)
    else:
        sos = Signoff.objects.filter(locale=locale, appversion=ms.appver)
    try:
        return sos.order_by('-pk')[0]
    except IndexError:
        return None

def _get_total_pushes(locale=None, mstone=None):
    if mstone:
        forest = mstone.appver.tree.l10n
        repo_url = '%s%s/' % (forest.url, locale.code) 
        return Push.objects.filter(repository__url=repo_url).count()
    else:
        return Push.objects.count()

def _get_compare_locales_result(rev, tree):
        try:
            return Run.objects.filter(revisions=rev,
                                      tree=tree).order_by('-build__id')[0]
        except:
            return None

def _get_api_items(locale, appver=None, current=None, start=0, offset=10):
    if appver:
        forest = appver.tree.l10n
        repo_url = '%s%s/' % (forest.url, locale.code) 
        pushobjs = Push.objects.filter(repository__url=repo_url).order_by('-push_date')[start:start+offset]
    else:
        pushobjs = Push.objects.order_by('-push_date')[start:start+offset]
    
    pushes = []
    for pushobj in pushobjs:
        if appver:
            signoff_trees = [appver.tree]
        else:
            signoff_trees = Tree.objects.filter(l10n__repositories=pushobj.repository, appversion__milestone__isnull=False)
        name = '%s on [%s]' % (pushobj.user, pushobj.push_date)
        date = pushobj.push_date.strftime("%Y-%m-%d")
        cur = current and current.push.id == pushobj.id

        # check compare-locales
        runs2 = Run.objects.filter(revisions=pushobj.tip)
        for tree in signoff_trees:
            try:
                lastrun = runs2.filter(tree=tree).order_by('-build__id')[0]
                missing = lastrun.missing + lastrun.missingInFiles
                cmp_segs = []
                if lastrun.errors:
                    cmp_segs.append('%d error(s)' % lastrun.errors)
                if missing:
                    cmp_segs.append('%d missing' % missing)
                if lastrun.obsolete:
                    cmp_segs.append('%d obsolete' % lastrun.obsolete)
                if cmp_segs:
                    compare = ', '.join(cmp_segs)
                else:
                    compare = 'green (%d%%)' % lastrun.completion
            except:
                compare = 'no build'

            pushes.append({'name': name,
                           'date': date,
                           'time': pushobj.push_date.strftime("%H:%M:%S"),
                           'id': pushobj.id,
                           'user': pushobj.user,
                           'revision': pushobj.tip.shortrev,
                           'revdesc': pushobj.tip.description,
                           'status': 'green',
                           'build': 'green',
                           'compare': compare,
                           'signoff': cur,
                           'url': '%spushloghtml?changeset=%s' % (pushobj.repository.url, pushobj.tip.shortrev),
                           'accepted': current.accepted if cur else None})
    return pushes

def _get_current_js(cur):
    current = {}
    if cur:
        current['when'] = cur.when.strftime("%Y-%m-%d %H:%M")
        current['author'] = str(cur.author)
        current['status'] = None if cur.status==0 else cur.accepted
        current['id'] = str(cur.id)
        current['class'] = cur.flag
    return current

def _get_notes(session):
    notes = {}
    for i in ('info','warning','error'):
        notes[i] = session.get('signoff_%s' % (i,), None)
        if notes[i]:
            del session['signoff_%s' % (i,)]
        else:
            del notes[i]
    return notes

def _get_push_offset(id, shift=0):
    """returns an offset of the push for signoff slider"""
    if not id:
        return 0
    push = Push.objects.get(changesets__revision__startswith=id)
    num = Push.objects.filter(pk__gt=push.pk, repository__url=push.repository.url).count()
    if num+shift<0:
        return 0
    return num+shift

def _get_accepted_signoff(locale, ms=None, av=None):
    '''this function gets the latest accepted signoff
    for a milestone/locale
    '''

    return _signoffs(ms is None and av or ms, locale=locale.code)


def _signoffs(appver_or_ms, status=1, getlist=False, locale=None):
    '''Get the signoffs for a milestone, or for the appversion as
    queryset (or manager).
    By default, returns the accepted ones, which can be overwritten to
    get any (status=None) or a particular status.

    If the locale argument is given, return the latest signoff with the
    requested status, or None.

    If getlist=True is specified, returns a dictionary mapping locale
    codes to a list of statuses, all that are newer than the
    latest obsolete action or accepted signoff (the latter is included).
    '''
    if isinstance(appver_or_ms, Milestone):
        ms = appver_or_ms
        if ms.status==2:
            assert not getlist
            return ms.signoffs
        appver = ms.appver
    else:
        appver = appver_or_ms

    sos = Signoff.objects.filter(appversion=appver)
    if locale is not None:
        sos = sos.filter(locale__code=locale)
    sos = sos.annotate(latest_action=Max('action__id'))
    sos_vals = list(sos.values('locale__code','id','latest_action'))
    actions = Action.objects
    actionflags=dict(actions.filter(id__in=map(lambda d: d['latest_action'],
                                               sos_vals)).values_list('id','flag'))
    actionflags[0] = 0 # migrated pending signoffs lack any action :-(
    if getlist:
        lf = defaultdict(list)
    else:
        lf = dict()
    for d in sos_vals:
        loc = d['locale__code']
        flag = actionflags[d['latest_action'] or 0]
        if flag == 4:
            # obsoleted, drop previous signoffs
            lf.pop(loc, None)
        else:
            if getlist:
                if flag == 1:
                    # approved, forget previous
                    lf[loc] = [flag]
                else:
                    lf[loc].append(flag)
            else:
                if status is not None:
                    if flag == status:
                        lf[loc] = d['id']
                else:
                    lf[loc] = d['id']

    if getlist:
        if locale is not None:
            return lf[loc]
        return lf
    if locale is not None:
        try:
            return sos.get(id=lf[loc])
        except KeyError:
            return None
    return sos.filter(id__in=lf.values())