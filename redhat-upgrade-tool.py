#!/usr/bin/python
#
# redhat-upgrade-tool.py - commandline frontend for redhat-upgrade-tool, the
# Red Hat Upgrade Tool.
#
# Copyright (C) 2012 Red Hat Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Author: Will Woods <wwoods@redhat.com>

import os
import re
import shlex
import sys, time, platform, shutil, signal
from subprocess import CalledProcessError, Popen, PIPE
from ConfigParser import NoOptionError, RawConfigParser

from redhat_upgrade_tool.util import call, check_call, check_output, rm_f, mkdir_p, rlistdir, kernelver
from redhat_upgrade_tool.download import UpgradeDownloader, YumBaseError, yum_plugin_for_exc, URLGrabError
from redhat_upgrade_tool.sysprep import prep_upgrade, prep_boot, setup_media_mount, setup_cleanup_post, disable_old_repos, Config
from redhat_upgrade_tool.sysprep import modify_repos, remove_cache, reset_boot
from redhat_upgrade_tool.boot import upgrade_boot_args
from redhat_upgrade_tool.rollback import snapshot_metadata_file, rhel6_profile
from redhat_upgrade_tool.rollback.bootloader import boom_cleanup, restore_boot, create_boot_entry, restore_grub_conf, backup_boot_files, change_boot_entry, clean_snapshot_boot_files, clean_target_boot_files, clean_grub2, clean_target_kdump
from redhat_upgrade_tool.rollback.snapshot import LVM, SnapshotError
from redhat_upgrade_tool.rollback.preparecleanup import create_cleanup_script, dump_target_kernelver
from redhat_upgrade_tool.rollback.cleanup_script import clean_rut_boot_dirs
from redhat_upgrade_tool.upgrade import RPMUpgrade, TransactionError

from redhat_upgrade_tool.commandline import parse_args, do_cleanup, device_setup
from redhat_upgrade_tool import textoutput as output
from redhat_upgrade_tool import upgradeconf
from redhat_upgrade_tool import rhel_gpgkey_path
from redhat_upgrade_tool import preupgrade_script_path
from redhat_upgrade_tool import release_version_file
from redhat_upgrade_tool import _, kernelpath, initrdpath
from redhat_upgrade_tool import grub_conf_file
from redhat_upgrade_tool import MIN_AVAIL_BYTES_FOR_BOOT

import redhat_upgrade_tool.logutils as logutils
import redhat_upgrade_tool.media as media

from preupg.xccdf import XccdfHelper
from preupg import settings

import logging
log = logging.getLogger("redhat-upgrade-tool")


def message(m):
    print m
    log.info(m)


def setup_downloader(version, instrepo=None, cacheonly=False, repos=[],
                     enable_plugins=[], disable_plugins=[], noverifyssl=False):
    log.debug("setup_downloader(version=%s, repos=%s)", version, repos)
    f = UpgradeDownloader(version=version, cacheonly=cacheonly)
    f.preconf.enabled_plugins += enable_plugins
    f.preconf.disabled_plugins += disable_plugins
    f.instrepoid = instrepo
    repo_cb = output.RepoCallback()
    repo_prog = output.RepoProgress(fo=sys.stderr)
    disabled_repos = f.setup_repos(callback=repo_cb,
                                   progressbar=repo_prog,
                                   repos=repos,
                                   noverifyssl=noverifyssl)
    disabled_repos = filter(lambda id: id != f.instrepoid, disabled_repos)
    if disabled_repos:
        print _("No upgrade available for the following repos") + ": " + \
                " ".join(disabled_repos)
        print _("Check that the repo URLs are correct.")
        log.info("disabled repos: " + " ".join(disabled_repos))
    return f


def download_packages(f):
    updates = f.build_update_transaction(callback=output.DepsolveCallback(f))
    # check for empty upgrade transaction
    if not updates:
        print _('No upgrade found, please check the repository specified is correct.')
        print _('Finished. Nothing to do.')
        raise SystemExit(0)
    # print dependency problems before we start the upgrade
    transprobs = f.describe_transaction_problems()
    if transprobs and not major_upgrade:
        print "WARNING: potential problems with upgrade"
        for p in transprobs:
            print "  " + p
    # clean out any unneeded packages from the cache
    f.clean_cache(keepfiles=(p.localPkg() for p in updates))
    # download packages
    f.download_packages(updates, callback=output.DownloadCallback())

    return updates


def transaction_test(pkgs):
    print _("testing upgrade transaction")
    pkgfiles = set(po.localPkg() for po in pkgs)
    fu = RPMUpgrade()
    probs = fu.setup_transaction(pkgfiles=pkgfiles, check_fatal=False)
    rv = fu.test_transaction(callback=output.TransactionCallback(numpkgs=len(pkgfiles)))
    return (probs, rv)


def reboot():
    call(['reboot'])


def get_preupgrade_result_name():
    return os.path.join(settings.assessment_results_dir,
                        settings.xml_result_name)


def check_preupg_target_system_version(treeinfo):
    if not os.path.exists(release_version_file):
        print _("First, run the Preupgrade Assistant to analyze the system.")
        raise SystemExit(1)
    try:
        with open(release_version_file) as release_file:
            release = release_file.readlines()
    except (IOError, OSError) as e:
        print _("Unable to read the %s file provided by the Preupgrade"
                " Assistant:\n %s") % (release_version_file, e)
        raise SystemExit(1)
    try:
        preupg_supported_sysver = release[1].strip()
    except KeyError:
        print _("Error: The %s file provided by the Preupgrade Assistant has"
                " incorrect content.") % release_version_file
        raise SystemExit(1)
    installation_media_sysver = treeinfo.get('general', 'version')
    if preupg_supported_sysver != installation_media_sysver:
        print _("The installed version of Preupgrade Assistant allows upgrade"
                " only to the system version %s." % preupg_supported_sysver)
        raise SystemExit(1)

def is_clean_safe(snapshots):
    '''
    Do not allow performing --clean and --clean-snapshots options when booted into RHEL 6 snapshot.
    '''
    if not snapshots:
        return True
    with open('/proc/cmdline') as cmdline:
        res = re.search("root=(\S*)", cmdline.read())
        if res is None:
            print _("Error: unable to locate kernel root arg")
            raise SystemExit(1)
        root = res.group(1)
        for snapshot in snapshots.values():
            if root == snapshot.full_path:
                return False
    return True


def main(args):
    global major_upgrade

    try:
        lvm = LVM(args.snapshot_root_lv, args.snapshot_lv, conf_path=snapshot_metadata_file)
    except SnapshotError as exc:
        print _(exc)
        raise SystemExit(1)

    if args.snapshot_root_lv:
        if platform.machine() != "x86_64":
            print _("Error: Rollbacks are possible only on the x86_64 architecture.")
            raise SystemExit(1)
        create_cleanup_script()

    if args.system_restore:
        # TODO: .... add checks, exceptions, ....
        lvm.restore_snapshots()
        boom_cleanup(rhel6_profile)
        restore_boot()

        if args.reboot:
            reboot()
        else:
            print _('Preparation for recovery finished.'
                    ' Reboot to recover original system.')
        return

    if args.clean_snapshots:
        if not is_clean_safe(lvm.snapshots):
            print _("Error: cannot process --clean-snapshots option from booted snapshot, to rollback system use --system-restore option")
            raise SystemExit(1)

        lvm.remove_snapshots()
        boom_cleanup(rhel6_profile)
        clean_snapshot_boot_files()
        restore_grub_conf()
        return

    if not lvm.create_snapshots():
        print _("Error: could not create snapshot(s).")
        raise SystemExit(1)

    root_snapshot = lvm.get_root_snapshot()
    if root_snapshot is not None and root_snapshot.exists:
        # back up boot files & grub.conf before we touch the grub
        backup_boot_files()

        if not create_boot_entry("RHEL 6 Snapshot", rhel6_profile, root_snapshot.lv):
            print _("Error: could not create a boot entry for the snapshot.")
            raise SystemExit(1)

        if not change_boot_entry():
            print _("Error: could not change boot entry created by boom.")
            raise SystemExit(1)

    if args.clean:
        if not is_clean_safe(lvm.snapshots):
            print _("Error: cannot process --clean option from snapshot, to rollback system use --system-restore option")
            raise SystemExit(1)

        do_cleanup(args)
        lvm.remove_snapshots()
        boom_cleanup(rhel6_profile)
        clean_snapshot_boot_files()
        clean_target_boot_files()
        clean_target_kdump()
        restore_grub_conf()
        clean_grub2()
        clean_rut_boot_dirs()
        return
    else:
        # Leaving cache from previous runs of the tool could foil the correct
        # download of packages for upgrade (bz#1303982)
        remove_cache()

    if args.device or args.iso:
        device_setup(args)

    # Get our packages set up where we can use 'em
    print _("setting up repos...")
    f = setup_downloader(version=args.network,
                         cacheonly=args.cacheonly,
                         instrepo=args.instrepo,
                         repos=args.repos,
                         enable_plugins=args.enable_plugins,
                         disable_plugins=args.disable_plugins,
                         noverifyssl=args.noverifyssl)

    if not args.force:
        check_preupg_target_system_version(f.treeinfo)

    if is_major_version_upgrade(f.treeinfo):
        major_upgrade = True
        if not args.force:
            check_preupg_risks()
        else:
            log.info("Skipping examining the Preupgrade Assistant results.")

    if not args.force:
        check_same_variant_upgrade(f.treeinfo)
    else:
        log.info("Skipping system variant check.")

    if args.nogpgcheck:
        f._override_sigchecks = True
    elif not f.instrepo.gpgcheck:
        # If instrepo is a Red Hat repo, add the gpg key and reload the repos
        try:
            key = "product"
            if not f.treeinfo.has_section(key):
                key = "release"
            if f.treeinfo.get(key, 'name') == 'Red Hat Enterprise Linux':
                log.info("Reloading repos with GPG key")
                args.repos.append(('gpgkey', '%s=%s' % (f.instrepo.name, rhel_gpgkey_path)))
                f = setup_downloader(version=args.network,
                                     cacheonly=args.cacheonly,
                                     instrepo=args.instrepo,
                                     repos=args.repos,
                                     enable_plugins=args.enable_plugins,
                                     disable_plugins=args.disable_plugins,
                                     noverifyssl=args.noverifyssl)
        except NoOptionError:
            log.debug("No product name found, skipping gpg check")

    if args.expire_cache:
        print "expiring cache files"
        f.cleanExpireCache()
        return
    if args.clean_metadata:
        print "cleaning metadata"
        f.cleanMetadata()
        return

    # Cleanup old conf files
    log.info("Clearing %s", upgradeconf)
    rm_f(upgradeconf)
    mkdir_p(os.path.dirname(upgradeconf))

    # TODO: error msg generation should be shared between CLI and GUI
    if args.skipkernel:
        message("skipping kernel/initrd download")
    elif f.instrepoid is None or f.instrepoid in f.disabled_repos:
        print _("Error: can't get boot images.")
        if args.instrepo:
            print _("The '%s' repo was rejected by yum as invalid.") % args.instrepo
            if args.iso:
                print _("The given ISO probably isn't an install DVD image.")
                media.umount(args.device.mnt)
            elif args.device:
                print _("The media doesn't contain a valid install DVD image.")
        else:
            print _("The installation repo isn't currently available.")
            print _("Try again later, or specify a repo using --instrepo.")
        raise SystemExit(1)
    else:
        print _("getting boot images...")
        kernel, initrd = f.download_boot_images() # TODO: force arch?
        if args.snapshot_root_lv:
            # In case of rollback, we want to be able to remove kernel files
            # of target (upgraded) system after the rollback and be sure that
            # we will not remove those that possibly exists
            kv = kernelver(kernel)
            if not kv:
                print _("Warning: cannot determine version of target kernel."
                        " In case of rollback, you will need to remove kernel"
                        " files of target kernel manually.")
            elif os.path.isfile("/boot/vmlinuz-%s" % kv):
                print _("Warning: detected kernel files matching with"
                        " kernel of target system. In case of rollback, you"
                        " would need to clean these files manually.")
            else:
                dump_target_kernelver(kv)

    if args.skippkgs:
        message("skipping package download")
    else:
        print _("setting up update...")
        if len(f.pkgSack) == 0:
            print("no updates available in configured repos!")
            raise SystemExit(1)
        pkgs = download_packages(f)
        # Run a test transaction
        probs, rv = transaction_test(pkgs)

    # And prepare for upgrade
    # TODO: use polkit to get root privs for these things
    print _("setting up system for upgrade")
    if not args.skippkgs:
        prep_upgrade(pkgs)

    # Disable the RHEL-6 repos
    disable_old_repos()

    # Save the repo configuration
    f.save_repo_configs()

    # Dump all configuration to upgrade.conf, other tools need to know
    #TODO:some items are structured, would be nice to unpack them
    with Config(upgradeconf) as conf:
        argsdict = args.__dict__
        for arg in argsdict:
            conf.set("config", arg.__str__(), argsdict[arg].__str__())

    if args.cleanup_post:
        setup_cleanup_post()

    # Workaround the redhat-upgrade-dracut upgrade-post hook order problem
    # Copy upgrade.conf to /root/preupgrade so that it won't be removed
    # before the postupgrade scripts are run.
    mkdir_p('/root/preupgrade')
    shutil.copyfile(upgradeconf, '/root/preupgrade/upgrade.conf')

    # Run the preuprade scripts if present
    if os.path.isdir(preupgrade_script_path):
        scripts = sorted(rlistdir(preupgrade_script_path))
        failed_scripts = {}
        for s in scripts:
            if os.access(s, os.X_OK):
                try:
                    check_call(s)
                except CalledProcessError as e:
                    failed_scripts[s] = e.returncode
        if failed_scripts:
            print("Following preupgrade script(s) failed:\n")
            for key, val in failed_scripts.iteritems():
                print("%s exited with status %d" % (key, val))
            print('exiting')
            sys.exit(1)

    if not args.skipbootloader:
        if args.skipkernel:
            print "warning: --skipkernel without --skipbootloader"
            print "using default paths: %s %s" % (kernelpath, initrdpath)
            kernel = kernelpath
            initrd = initrdpath
        upgrade_boot_args()
        prep_boot(kernel, initrd)

    # Check for available space in /boot/ needed for kernel and grub
    # installation during the upgrade.
    statvfs = os.statvfs("/boot")
    avail_bytes = statvfs.f_frsize * statvfs.f_bavail
    if not args.no_space_check and avail_bytes < MIN_AVAIL_BYTES_FOR_BOOT:
        reset_boot()
        additional_mib_needed = \
            (MIN_AVAIL_BYTES_FOR_BOOT - avail_bytes) / 2**20
        sys.stderr.write(_("Not enough space. /boot/ needs additional %d MiB"
                           ".\n") % additional_mib_needed)
        raise SystemExit(1)

    # Replace temporary media paths
    modify_repos(args)

    if args.device:
        setup_media_mount(args.device, args.iso)

    if args.iso:
        media.umount(args.device.mnt)

    if args.reboot:
        reboot()
    else:
        print _('Finished. Reboot to start upgrade.')

    # --- Here's where we summarize potential problems. ---

    # list packages without updates, if any
    missing = sorted(f.find_packages_without_updates(), key=lambda p:p.nevra)
    if missing and not major_upgrade:
        message(_('Packages without updates:'))
        for p in missing:
            message("  %s" % p)

    # warn if the "important" repos are disabled
    #if f.disabled_repos:
        # NOTE: I hate having a hardcoded list of Important Repos here.
        # This information should be provided by the system, somehow..
        #important = ("fedora", "updates")
        #if any(i in f.disabled_repos for i in important):
        #    msg = _("WARNING: Some important repos could not be contacted: %s")
        #else:
        #    msg = _("NOTE: Some repos could not be contacted: %s")
        #print msg % ", ".join(f.disabled_repos)
        #print _("If you start the upgrade now, packages from these repos will not be installed.")

    # warn about broken dependencies etc.
    # If this is a major version upgrade, the user has already been warned
    # about all of this from preupgrade-assistant, so skip the warning here
    if probs and not major_upgrade:
        print
        print _("WARNING: problems were encountered during transaction test:")
        for s in probs.summaries:
            print "  "+s.desc
            for line in s.format_details():
                print "    "+line
        print _("Continue with the upgrade at your own risk.")


def is_major_version_upgrade(treeinfo):
    """Compare the first part of the version number in the treeinfo with the
    first part of the version number of the system to determine if this is a
    major version upgrade.
    """
    return treeinfo.get('general', 'version').split('.')[0] != \
        platform.linux_distribution()[1].split('.')[0]


def check_preupg_risks():
    returncode = XccdfHelper.check_inplace_risk(get_preupgrade_result_name(), 0)
    if int(returncode) == 0:
        print _("The Preupgrade Assistant hasn't found any risks.\n"
                "Upgrade will continue.")
    elif int(returncode) == 1:
        print _("The Preupgrade Assistant has found upgrade risks.\n"
                " You can run 'preupg --riskcheck --verbose' to view"
                " these risks.\nAddressing high risk issues is"
                " mandatory before continuing with the upgrade.\n"
                "Ignoring these risks may result in a broken and/or"
                " unsupported upgrade.\nPlease backup your data.\n\n"
                "List of issues:")

        XccdfHelper.check_inplace_risk(get_preupgrade_result_name(), verbose=2)

        # Python 2.6 raises EOFError if raw_input receives a SIGWINCH.
        # Try to tell the difference between that and a real EOF.

        global sigwinch
        sigwinch = False
        def handle_sigwinch(signum, frame):
            global sigwinch
            sigwinch = True
        orig_handler = signal.signal(signal.SIGWINCH, handle_sigwinch)

        while True:
            try:
                sigwinch = False
                answer = raw_input(_("Continue with the upgrade [Y/N]? "))
                break
            except EOFError:
                if sigwinch:
                    # Not a real EOF, try again
                    print
                    continue
                else:
                    # Real EOF, exit
                    answer = ''
                    break
        signal.signal(signal.SIGWINCH, orig_handler)

        # TRANSLATORS: y for yes
        if answer.lower() != _('y'):
            raise SystemExit(1)
    elif int(returncode) == 2:
        print _("The Preupgrade Assistant has found EXTREME upgrade"
                " risks.\nRun preupg --riskcheck --verbose to view\n"
                " these risks.\nContinuing with this upgrade is not\n"
                " recommended - the system will be unsupported\n"
                " and most likely broken after the upgrade.")
        raise SystemExit(1)
    else:
        print _("The Preupgrade Assistant has not been run.\n"
                "To upgrade the system, run preupg first.")
        raise SystemExit(1)


def check_same_variant_upgrade(treeinfo):
    distro = platform.linux_distribution()[0]
    if not distro.startswith("Red Hat Enterprise Linux "):
        print _("Invalid distribution: %s") % distro
        raise SystemExit(1)

    from_variant = distro[len('Red Hat Enterprise Linux '):]
    try:
        to_variant = treeinfo.get('general', 'variant')
    except NoOptionError:
        print _("Upgrade repository is not a Red Hat Enterprise Linux repository")
        raise SystemExit(1)

    if from_variant != to_variant:
        print _("Upgrade requested from Red Hat Enterprise Linux %s to %s") % (from_variant, to_variant)
        print _("Upgrades between Red Hat Enterprise Linux variants is not supported.")
        raise SystemExit(1)


if __name__ == '__main__':
    args = parse_args()
    major_upgrade = False

    # TODO: use polkit to get privs for modifying bootloader stuff instead
    if os.getuid() != 0:
        print _("you must be root to run this program.")
        raise SystemExit(1)

    # set up logging
    if args.debuglog:
        logutils.debuglog(args.debuglog)
    logutils.consolelog(level=args.loglevel)
    log.info("%s starting at %s", sys.argv[0], time.asctime())

    try:
        exittype = "cleanly"
        main(args)
    except KeyboardInterrupt as e:
        print
        log.info("exiting on keyboard interrupt")
        message(_("Exiting on keyboard interrupt"))
        raise SystemExit(1)
    except (YumBaseError, URLGrabError) as e:
        print
        if hasattr(e, "value") and isinstance(e.value, list):
            err = e.value.pop(0)
            message(_("Downloading failed: %s") % err)
            for p in e.value:
                message("  %s" % p)
        else:
            message(_("Downloading failed: %s") % e)
        log.debug("Traceback (for debugging purposes):", exc_info=True)
        raise SystemExit(2)
    except TransactionError as e:
        print
        message(_("Upgrade test failed with the following problems:"))
        for s in e.summaries:
            message(s)
        log.debug("Detailed transaction problems:")
        for p in e.problems:
            log.debug(p)
        log.error(_("Upgrade test failed."))
        raise SystemExit(3)
    except Exception as e:
        pluginfile = yum_plugin_for_exc()
        if pluginfile:
            plugin, ext = os.path.splitext(os.path.basename(pluginfile))
            log.error(_("The '%s' yum plugin has crashed.") % plugin)
            log.error(_("Please report this problem to the plugin developers:"),
                      exc_info=True)
            raise SystemExit(1)
        log.info("Exception:", exc_info=True)
        exittype = "with unhandled exception"
        raise
    finally:
        log.info("%s exiting %s at %s", sys.argv[0], exittype, time.asctime())
