#!/usr/bin/env python3
"""Fail the build when a backup mechanism can no longer satisfy the backup policy.

cluster/base/infrastructure/34-backup/backup-policy.yaml (ADR-011) states what has
to be recoverable and how well, and runs nothing. The mechanisms that do the work
live elsewhere: Longhorn RecurringJobs, a dump CronJob in each data-owning
namespace, backup-retention-config. This check binds the two. It fails when:

  - a dataset's producer runs less often than the dataset's RPO allows
  - a producer writes somewhere other than where its dataset says
  - a Longhorn dataset's claim does not exist, or is also marked not-backed-up
  - a retain count is below what a profile requires, locally or in the vault
  - a database dataset has no restore check, so "it replayed" would count as proof
  - the tables themselves are malformed or refer to things that do not exist

Without it the policy is a description that silently stops being true the first
time someone edits a schedule.

Run from the repository root:  python3 scripts/check-backup-policy.py
"""
import glob
import re
import sys

import yaml

BASE = 'cluster/base/infrastructure'
POLICY = BASE + '/34-backup/backup-policy.yaml'
RETENTION = BASE + '/34-backup/retention-config.yaml'
RECURRING = BASE + '/34-backup/longhorn-recurring-jobs.yaml'

# The source scheme each engine must use.
ENGINES = {'longhorn': 'pvc', 'postgres': 's3', 'sqlite': 's3'}
CRITICALITIES = {'critical', 'important', 'reconstructable'}
LOCAL_TIERS = ('daily', 'weekly', 'monthly')
REMOTE_TIERS = ('weekly', 'monthly')

errors = []


def err(msg):
    errors.append(msg)


def load_docs(path):
    with open(path, encoding='utf-8') as f:
        return [d for d in yaml.safe_load_all(f) if isinstance(d, dict)]


def rows(text):
    """Non-empty lines of a policy table, comments stripped."""
    out = []
    for line in (text or '').splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            out.append(line)
    return out


def duration(value):
    m = re.fullmatch(r'(\d+)([hd])', value)
    if not m:
        return None
    return int(m.group(1)) * (3600 if m.group(2) == 'h' else 86400)


def human(seconds):
    # Days only from two upward, so an RPO written as 24h is reported as 24h.
    if seconds % 86400 == 0 and seconds >= 2 * 86400:
        return str(seconds // 86400) + 'd'
    return str(seconds // 3600) + 'h'


def cron_interval(expr):
    """Longest gap between two runs, for the shapes of cron this repo uses.

    Returns None for anything else, which the caller reports rather than skips:
    an unrecognised schedule is exactly the change this check exists to look at.
    """
    fields = (expr or '').split()
    if len(fields) != 5:
        return None
    minute, hour, dom, month, dow = fields
    if not minute.isdigit() or month != '*':
        return None
    if hour == '*' and dom == '*' and dow == '*':
        return 3600
    if not hour.isdigit():
        return None
    if dom == '*' and dow == '*':
        return 86400
    if dom == '*' and dow.isdigit():
        return 7 * 86400
    if dow == '*' and dom.isdigit():
        return 31 * 86400
    return None


def upload_dest(cronjob):
    spec = cronjob['spec']['jobTemplate']['spec']['template']['spec']
    for c in spec.get('containers', []):
        if c.get('name') != 'upload':
            continue
        for e in c.get('env', []):
            if e.get('name') == 'DEST_DIR':
                return e.get('value')
    return None


def main():
    policy = load_docs(POLICY)
    if len(policy) != 1 or policy[0].get('kind') != 'ConfigMap':
        print('ERROR: ' + POLICY + ' must hold exactly one ConfigMap')
        return 1
    data = policy[0].get('data') or {}
    for key in ('profiles', 'applications', 'datasets', 'restore-checks', 'not-backed-up'):
        if key not in data:
            err(POLICY + ': missing table ' + key)
    if errors:
        return report()

    # ---- profiles --------------------------------------------------------
    profiles = {}
    for row in rows(data['profiles']):
        cols = row.split()
        if len(cols) != 7:
            err('profiles: expected 7 columns, got ' + str(len(cols)) + ': ' + row)
            continue
        name = cols[0]
        if all(c == '-' for c in cols[1:]):
            profiles[name] = None  # no guarantees: GitOps is the recovery source
            continue
        p = {}
        for label, value in zip(('rpo', 'rto', 'restore_test', 'offsite'), cols[1:5]):
            p[label] = duration(value)
            if p[label] is None:
                err('profile ' + name + ': ' + label + ' ' + repr(value) + ' is not a duration like 24h or 7d')
        local = re.fullmatch(r'(\d+)/(\d+)/(\d+)', cols[5])
        remote = re.fullmatch(r'(\d+)/(\d+)', cols[6])
        if not local:
            err('profile ' + name + ': local retention ' + repr(cols[5]) + ' must be daily/weekly/monthly, e.g. 7/3/3')
        if not remote:
            err('profile ' + name + ': remote retention ' + repr(cols[6]) + ' must be weekly/monthly, e.g. 1/3')
        p['local'] = tuple(int(x) for x in local.groups()) if local else (0, 0, 0)
        p['remote'] = tuple(int(x) for x in remote.groups()) if remote else (0, 0)
        if p['rpo'] and p['offsite'] and p['offsite'] < p['rpo']:
            err('profile ' + name + ': offsite ' + human(p['offsite']) + ' is shorter than the RPO '
                + human(p['rpo']) + ' -- a point cannot be verified offsite before it exists')
        profiles[name] = p

    # ---- applications ----------------------------------------------------
    apps = {}
    for row in rows(data['applications']):
        cols = row.split()
        if len(cols) != 3:
            err('applications: expected 3 columns, got ' + str(len(cols)) + ': ' + row)
            continue
        app, criticality, profile = cols
        if app in apps:
            err('applications: ' + app + ' is listed twice')
        if criticality not in CRITICALITIES:
            err('application ' + app + ': unknown criticality ' + repr(criticality))
        if profile not in profiles:
            err('application ' + app + ': unknown profile ' + repr(profile))
        apps[app] = profile

    # ---- not-backed-up ---------------------------------------------------
    excluded = set()
    for claim in rows(data['not-backed-up']):
        if claim in excluded:
            err('not-backed-up: ' + claim + ' is listed twice')
        excluded.add(claim)

    # ---- manifests the datasets point at ---------------------------------
    cronjobs = {}
    claims = set()
    for path in sorted(glob.glob('cluster/base/**/*.yaml', recursive=True)):
        with open(path, encoding='utf-8', errors='replace') as f:
            text = f.read()
        if 'kind: CronJob' not in text and 'kind: PersistentVolumeClaim' not in text:
            continue
        try:
            docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
        except yaml.YAMLError:
            continue  # unparseable manifests are other jobs' concern
        for d in docs:
            md = d.get('metadata') or {}
            if d.get('kind') == 'CronJob':
                cronjobs[(md.get('namespace'), md.get('name'))] = d
            elif d.get('kind') == 'PersistentVolumeClaim':
                claims.add((md.get('namespace'), md.get('name')))
    retention = load_docs(RETENTION)[0].get('data') or {}
    recurring = {d['metadata']['name']: d for d in load_docs(RECURRING) if d.get('kind') == 'RecurringJob'}

    # ---- datasets --------------------------------------------------------
    datasets = {}
    for row in rows(data['datasets']):
        cols = row.split()
        if len(cols) != 5:
            err('datasets: expected 5 columns, got ' + str(len(cols)) + ': ' + row)
            continue
        ds, app, engine, source, producer = cols
        if ds in datasets:
            err('datasets: ' + ds + ' is listed twice')
        scheme, _, location = source.partition(':')
        datasets[ds] = {'app': app, 'engine': engine, 'location': location}
        where = 'dataset ' + ds

        if app not in apps:
            err(where + ': unknown application ' + repr(app))
            continue
        profile = profiles.get(apps[app])
        if profile is None:
            err(where + ': application ' + app + ' is reconstructable, so it has no datasets to protect')
            continue
        if engine not in ENGINES:
            err(where + ': unknown engine ' + repr(engine))
            continue
        if scheme != ENGINES[engine]:
            err(where + ': a ' + engine + ' dataset needs a ' + ENGINES[engine] + ': source, got ' + repr(source))
            continue

        kind, _, ref = producer.partition(':')
        schedule = None
        if engine == 'longhorn':
            if kind != 'recurringjob':
                err(where + ': a longhorn dataset is produced by a recurringjob:, got ' + repr(producer))
                continue
            job = recurring.get(ref)
            if not job:
                err(where + ': RecurringJob ' + ref + ' does not exist in ' + RECURRING)
                continue
            if job['spec'].get('task') != 'backup':
                err(where + ': ' + ref + ' is a ' + str(job['spec'].get('task'))
                    + ' job -- a snapshot never leaves the node, so it is not a recovery point')
            if 'default' not in (job['spec'].get('groups') or []):
                err(where + ': ' + ref + ' does not cover the default group, which is where every protected volume is')
            schedule = retention.get(ref.replace('-', '_') + '_cron')
            namespace, _, claim = location.partition('/')
            if claim in excluded:
                err(where + ': ' + claim + ' is also in not-backed-up; it cannot be both')
            if (namespace, claim) not in claims and (None, claim) not in claims:
                err(where + ': no PersistentVolumeClaim ' + namespace + '/' + claim + ' in cluster/base')
        else:
            if kind != 'cronjob':
                err(where + ': a ' + engine + ' dataset is produced by a cronjob:, got ' + repr(producer))
                continue
            namespace, _, name = ref.partition('/')
            job = cronjobs.get((namespace, name))
            if not job:
                err(where + ': CronJob ' + ref + ' does not exist in cluster/base')
                continue
            schedule = job['spec'].get('schedule')
            dest = upload_dest(job)
            want = 's3://' + location.rstrip('/')
            if (dest or '').rstrip('/') != want:
                err(where + ': ' + ref + ' uploads to ' + str(dest) + ', but the dataset says ' + want)

        interval = cron_interval(schedule)
        if interval is None:
            err(where + ': cannot work out how often ' + repr(schedule) + ' runs -- extend cron_interval() '
                'rather than skipping the check')
        elif interval > profile['rpo']:
            err(where + ': produced every ' + human(interval) + ', which cannot meet a '
                + human(profile['rpo']) + ' RPO')

    for app, profile in apps.items():
        if profiles.get(profile) is not None and not any(d['app'] == app for d in datasets.values()):
            err('application ' + app + ' has the ' + profile + ' profile and no dataset: a guarantee with nothing behind it')

    # ---- retention -------------------------------------------------------
    guaranteed = [p for p in profiles.values() if p]
    for i, tier in enumerate(LOCAL_TIERS):
        need = max((p['local'][i] for p in guaranteed), default=0)
        key = 'backup_' + tier + '_retain'
        if 'backup-' + tier not in recurring and need:
            err('profiles require ' + str(need) + ' local ' + tier + ' backups, and there is no backup-' + tier + ' RecurringJob')
        have = retention.get(key)
        if have is None or not str(have).isdigit():
            if need:
                err(RETENTION + ': ' + key + ' is missing, and profiles require ' + str(need) + ' ' + tier + ' backups')
        elif int(have) < need:
            err(RETENTION + ': ' + key + ' is ' + str(have) + ', below the ' + str(need) + ' local ' + tier
                + ' backups a profile requires')
    # The vault is a mirror of local state plus the reconciler's grace period:
    # it keeps whatever local keeps, never fewer. So a vault minimum is met by
    # the local retain count, and one above it cannot be met at all.
    for i, tier in enumerate(REMOTE_TIERS):
        need = max((p['remote'][i] for p in guaranteed), default=0)
        have = retention.get('backup_' + tier + '_retain')
        if need and (have is None or not str(have).isdigit() or int(have) < need):
            err('profiles require ' + str(need) + ' ' + tier + ' backups in the vault, which only ever holds what '
                'local retention keeps (backup_' + tier + '_retain=' + str(have) + ')')

    # ---- restore checks --------------------------------------------------
    checked = set()
    for row in rows(data['restore-checks']):
        parts = row.split(None, 2)
        if len(parts) != 3:
            err('restore-checks: expected dataset, min, query: ' + row)
            continue
        ds, minimum, query = parts
        if ds not in datasets:
            err('restore-checks: unknown dataset ' + repr(ds))
            continue
        if datasets[ds]['engine'] == 'longhorn':
            err('restore-checks: ' + ds + ' is a volume; the restore-test checks those itself (fsck, mount)')
        if not minimum.isdigit():
            err('restore-checks: ' + ds + ': min ' + repr(minimum) + ' is not a whole number')
        if not query.lower().startswith('select '):
            err('restore-checks: ' + ds + ': only read-only select queries belong here: ' + query)
        checked.add(ds)
    for ds, d in datasets.items():
        if d['engine'] in ('postgres', 'sqlite') and ds not in checked:
            err('dataset ' + ds + ': no restore-checks -- "it replayed without error" does not show the data came back')

    return report(len(datasets), len(apps))


def report(n_datasets=0, n_apps=0):
    if errors:
        print('Backup policy contract: ' + str(len(errors)) + ' problem(s)')
        for e in errors:
            print('  ERROR: ' + e)
        return 1
    print('Backup policy contract OK: ' + str(n_datasets) + ' datasets across ' + str(n_apps) + ' applications')
    return 0


if __name__ == '__main__':
    sys.exit(main())
