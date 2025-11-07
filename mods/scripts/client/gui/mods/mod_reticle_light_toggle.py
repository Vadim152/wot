# -*- coding: utf-8 -*-
"""Reticle light toggle mod for World of Tanks (Lesta).

This module installs a controller that listens for right mouse button presses
in battle and toggles the color of the gun marker (reticle) between the base
and the alternate color configured by the user.  A small configuration window
is integrated with the official Mod Hub settings button (modsListApi) so the
player can tweak the colors directly from the hangar.
"""
from __future__ import absolute_import, division, print_function

import json
import os
import weakref

import BigWorld
import Keys

from account_helpers.settings_core.settings_constants import CROSSHAIR_PANEL
from debug_utils import LOG_CURRENT_EXCEPTION, LOG_NOTE
from helpers import dependency
from skeletons.account_helpers.settings_core import ISettingsCore
from skeletons.gui.app_loader import IAppLoader
from skeletons.gui.battle_session import IBattleSessionProvider

MOD_ID = 'reticleLightToggle'
MOD_NAME = 'Переключатель света прицела'
MOD_DESCRIPTION = 'Переключает цвет прицела по ПКМ и позволяет настраивать цвета.'
CONFIG_DIRECTORY = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../../configs/reticle_light_toggle'))
CONFIG_PATH = os.path.join(CONFIG_DIRECTORY, 'settings.json')


DEFAULT_CONFIG = {
    'enabled': True,
    'baseColor': '#FFCC00',
    'alternateColor': '#00FFDE',
    'startWithAlternateColor': False,
}


def _ensureConfigDirectory():
    if not os.path.isdir(CONFIG_DIRECTORY):
        try:
            os.makedirs(CONFIG_DIRECTORY)
        except Exception:  # pragma: no cover - directory creation is best effort
            LOG_CURRENT_EXCEPTION('%s: failed to create config directory' % MOD_ID)


class _ModConfig(object):
    __slots__ = ('enabled', 'baseColor', 'alternateColor', 'startWithAlternateColor')

    def __init__(self):
        for key, value in DEFAULT_CONFIG.items():
            setattr(self, key, value)
        self.load()

    def load(self):
        _ensureConfigDirectory()
        if not os.path.isfile(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, 'r') as fp:
                data = json.load(fp)
        except Exception:
            LOG_CURRENT_EXCEPTION('%s: unable to read config, using defaults' % MOD_ID)
            return
        for key in DEFAULT_CONFIG:
            if key in data:
                setattr(self, key, data[key])

    def save(self):
        _ensureConfigDirectory()
        data = {key: getattr(self, key) for key in DEFAULT_CONFIG}
        try:
            with open(CONFIG_PATH, 'w') as fp:
                json.dump(data, fp, indent=4, sort_keys=True)
        except Exception:
            LOG_CURRENT_EXCEPTION('%s: unable to save config' % MOD_ID)

    def asDict(self):
        return {key: getattr(self, key) for key in DEFAULT_CONFIG}


class _ReticleColorController(object):
    settingsCore = dependency.descriptor(ISettingsCore)
    sessionProvider = dependency.descriptor(IBattleSessionProvider)

    def __init__(self, config):
        self._config = config
        self._useAlternate = bool(config.startWithAlternateColor)
        self._isInstalled = False
        self._pendingUpdate = False

    def install(self):
        if self._isInstalled:
            return
        self._isInstalled = True
        if self._config.enabled:
            self.applyCurrentColor()

    def uninstall(self):
        self._isInstalled = False

    def toggle(self):
        if not self._config.enabled:
            return
        self._useAlternate = not self._useAlternate
        LOG_NOTE('%s: toggled reticle color (alternate=%s)' % (MOD_ID, self._useAlternate))
        self.applyCurrentColor()

    def refreshFromConfig(self, config):
        self._config = config
        self._useAlternate = bool(config.startWithAlternateColor)
        self.applyCurrentColor()

    def applyCurrentColor(self):
        color = self._config.alternateColor if self._useAlternate else self._config.baseColor
        if not self._applyGunMarkerColor(color):
            self._pendingUpdate = True
            LOG_NOTE('%s: gun marker controller not ready, defer color update' % MOD_ID)
        else:
            self._pendingUpdate = False

    def onAvatarReady(self):
        if self._pendingUpdate:
            self.applyCurrentColor()

    def _applyGunMarkerColor(self, hexColor):
        controller = None
        if self.sessionProvider is not None:
            controller = getattr(self.sessionProvider.shared, 'crosshair', None)
        if controller is not None:
            # Crosshair controller API differs between versions.  We try the most
            # common methods in a safe order.
            try:
                if hasattr(controller, 'setOverrideReticleColor'):
                    controller.setOverrideReticleColor(hexColor)
                    return True
                if hasattr(controller, 'setReticleColor'):  # old API
                    controller.setReticleColor(hexColor)
                    return True
            except Exception:
                LOG_CURRENT_EXCEPTION('%s: failed to apply color through crosshair controller' % MOD_ID)
        # Fallback: push the color through settings so the game rebuilds the markers.
        return self._applyThroughSettings(hexColor)

    def _applyThroughSettings(self, hexColor):
        try:
            if self.settingsCore is None:
                return False
            options = self.settingsCore.options
            if options is None:
                return False
            colorSetting = options.getSetting(CROSSHAIR_PANEL.GUN_MARKER_COLOR)
            if colorSetting is None:
                return False
            values = colorSetting.getSystemValue()
            # The structure is typically {'arcade': '#rrggbb', 'sniper': '#rrggbb', ...}
            if isinstance(values, dict):
                for key in values:
                    values[key] = hexColor
            colorSetting.setSystemValue(values)
            colorSetting.apply()
            return True
        except Exception:
            LOG_CURRENT_EXCEPTION('%s: unable to write color through settings core' % MOD_ID)
        return False


class _RightClickHook(object):
    def __init__(self, controller):
        self.__controllerRef = weakref.ref(controller)
        self.__patchedClasses = []

    def install(self):
        from AvatarInputHandler import control_modes
        classes = []
        for name in ('ArcadeControlMode', 'SniperControlMode', 'StrategicControlMode', 'DualGunControlMode'):
            cls = getattr(control_modes, name, None)
            if cls is not None:
                classes.append(cls)
        for cls in classes:
            if hasattr(cls, 'handleMouseEvent') and cls not in self.__patchedClasses:
                original = cls.handleMouseEvent

                def makeWrapper(method):
                    def wrapper(instance, event):
                        if self.__shouldToggle(event):
                            controller = self.__controllerRef()
                            if controller is not None:
                                controller.toggle()
                                return True
                        return method(instance, event)
                    wrapper.__original__ = method
                    return wrapper

                cls.handleMouseEvent = makeWrapper(original)
                self.__patchedClasses.append(cls)

    def uninstall(self):
        for cls in self.__patchedClasses:
            if hasattr(cls, 'handleMouseEvent'):
                handler = cls.handleMouseEvent
                original = getattr(handler, '__original__', None)
                if original is not None:
                    cls.handleMouseEvent = original
        self.__patchedClasses = []

    @staticmethod
    def __shouldToggle(event):
        try:
            if event is None:
                return False
            if hasattr(event, 'isButtonDown') and event.isButtonDown():
                key = getattr(event, 'button', getattr(event, 'key', None))
                return key == Keys.KEY_RIGHTMOUSE
        except Exception:
            LOG_CURRENT_EXCEPTION('%s: error while processing mouse event' % MOD_ID)
        return False


class _ModEntryPoint(object):
    appLoader = dependency.descriptor(IAppLoader)

    def __init__(self):
        self._config = _ModConfig()
        self._controller = _ReticleColorController(self._config)
        self._rightClickHook = _RightClickHook(self._controller)
        self._registerHangarEntry()
        self._installBattleListeners()
        self._rightClickHook.install()
        self._controller.install()
        LOG_NOTE('%s: initialization complete' % MOD_ID)

    def _registerHangarEntry(self):
        try:
            from gui.modsListApi import g_modsListApi
        except Exception:
            LOG_NOTE('%s: modsListApi is not available; settings window disabled' % MOD_ID)
            g_modsListApi = None
        if g_modsListApi is None:
            return
        g_modsListApi.addMod(
            modId=MOD_ID,
            name=MOD_NAME,
            description=MOD_DESCRIPTION,
            callback=self.__openSettingsWindow,
            enabled=self._config.enabled,
        )

    def __openSettingsWindow(self):
        try:
            from gui.modsListApi import g_modsListApi
        except Exception:
            g_modsListApi = None
        if g_modsListApi is None:
            return
        controls = [
            {
                'type': 'CheckBox',
                'label': 'Включить мод',
                'setting': 'enabled',
                'value': self._config.enabled,
            },
            {
                'type': 'Color',
                'label': 'Основной цвет прицела',
                'setting': 'baseColor',
                'value': self._config.baseColor,
            },
            {
                'type': 'Color',
                'label': 'Альтернативный цвет прицела',
                'setting': 'alternateColor',
                'value': self._config.alternateColor,
            },
            {
                'type': 'CheckBox',
                'label': 'Начинать бой с альтернативным цветом',
                'setting': 'startWithAlternateColor',
                'value': self._config.startWithAlternateColor,
            },
        ]
        try:
            g_modsListApi.showSettings(modId=MOD_ID, title=MOD_NAME, controls=controls, callback=self.__onSettingsChanged)
        except Exception:
            LOG_CURRENT_EXCEPTION('%s: unable to open settings window via modsListApi' % MOD_ID)

    def __onSettingsChanged(self, data):
        if not isinstance(data, dict):
            return
        for key in DEFAULT_CONFIG:
            if key in data:
                setattr(self._config, key, data[key])
        self._config.save()
        self._controller.refreshFromConfig(self._config)

    def _installBattleListeners(self):
        loader = self.appLoader
        if loader is None:
            return
        try:
            loader.onGUISpaceEntered += self.__onGUISpaceEntered
        except Exception:
            LOG_CURRENT_EXCEPTION('%s: unable to subscribe to GUI space events' % MOD_ID)

    def __onGUISpaceEntered(self, spaceID):
        player = BigWorld.player() if hasattr(BigWorld, 'player') else None
        if player is None:
            return
        if spaceID == 1:  # Battle space
            try:
                player.onVehicleEnterWorld += self.__onAvatarReady
            except Exception:
                LOG_CURRENT_EXCEPTION('%s: failed to subscribe to vehicle enter event' % MOD_ID)
        else:
            try:
                player.onVehicleEnterWorld -= self.__onAvatarReady
            except Exception:
                pass

    def __onAvatarReady(self, *_, **__):
        self._controller.onAvatarReady()


_modInstance = None


def init():
    global _modInstance
    if _modInstance is None:
        _modInstance = _ModEntryPoint()


init()
