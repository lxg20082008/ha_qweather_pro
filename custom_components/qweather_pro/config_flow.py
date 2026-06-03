"""QWeather (和风天气) 配置流实现."""
from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.const import CONF_HOST, CONF_API_KEY
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .api import QWeatherAPI
from .const import (
    DOMAIN,
    CONF_USE_TOKEN,
    CONF_LOCATION_ID,
    CONF_HOURLYSTEPS,
    CONF_DAILYSTEPS,
    CONF_UPDATE_INTERVAL,
    CONF_PROJECT_ID,
    CONF_KEY_ID,
    CONF_PRIVATE_KEY,
    CONF_GIRD,
    CONF_CUSTOM_UI,
    DEFAULT_UPDATE_INTERVAL,
    LANGUAGE_MAP,
    LOGGER,
)

class QWeatherConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """处理和风天气的配置流."""

    VERSION = 1

    def __init__(self) -> None:
        """初始化临时变量."""
        self._temp_data: dict[str, Any] = {}
        self._discovered_locations: list[dict[str, Any]] = []
        self._generated_private_key: str | None = None
        self._generated_public_key: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> QWeatherOptionsFlow:
        """获取并关联选项流."""
        return QWeatherOptionsFlow()

    def _generate_key_pair_sync(self) -> tuple[str, str]:
        """同步生成 JWT 密钥对."""
        private_key = ed25519.Ed25519PrivateKey.generate()
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return private_bytes.decode('utf-8'), public_bytes.decode('utf-8')

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """初始配置步骤."""
        if user_input is not None:
            self._temp_data = user_input
            if user_input.get(CONF_USE_TOKEN):
                return await self.async_step_jwt_setup()
            # 开启地理位置搜索流程
            return await self._async_search_location(user_input)

        default_location = f"{round(self.hass.config.longitude, 2)},{round(self.hass.config.latitude, 2)}"
        
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST): selector.TextSelector(),
                vol.Required(CONF_LOCATION_ID, default=default_location): selector.TextSelector(),                                       
                vol.Required(CONF_USE_TOKEN, default=False): selector.BooleanSelector(),
                vol.Optional(CONF_API_KEY): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }),
        )

    async def async_step_jwt_setup(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """JWT 身份验证步骤."""
        if not self._generated_private_key:
            self._generated_private_key, self._generated_public_key = await self.hass.async_add_executor_job(
                self._generate_key_pair_sync
            )

        if user_input is not None:
            self._temp_data.update({
                **user_input, 
                CONF_PRIVATE_KEY: self._generated_private_key
            })
            return await self._async_search_location(self._temp_data)

        return self.async_show_form(
            step_id="jwt_setup",
            data_schema=vol.Schema({
                vol.Required(CONF_PROJECT_ID): selector.TextSelector(),
                vol.Required(CONF_KEY_ID): selector.TextSelector(),
            }),
            description_placeholders={"public_key": self._generated_public_key}
        )

    async def _async_search_location(self, config_data: dict[str, Any]) -> FlowResult:
        """核心搜索逻辑：验证 Host 并抓取城市候选项."""
        errors: dict[str, str] = {}
        session = async_get_clientsession(self.hass)
        
        user_host = config_data[CONF_HOST].strip()
        raw_loc = config_data[CONF_LOCATION_ID].strip()

        # 检查过期域名
        deprecated_domains = ["api.qweather.com", "devapi.qweather.com", "geoapi.qweather.com"]
        if any(domain in user_host for domain in deprecated_domains):
            errors["base"] = "api_host_deprecated"

        if not errors:
            api = QWeatherAPI(
                session=session,
                api_key=config_data.get(CONF_API_KEY),
                use_token=config_data.get(CONF_USE_TOKEN),
                project_id=config_data.get(CONF_PROJECT_ID),
                key_id=config_data.get(CONF_KEY_ID),
                private_key=config_data.get(CONF_PRIVATE_KEY),
                host=user_host
            )

            try:
                # 获取系统语言进行本地化搜索
                ha_lang = self.hass.config.language
                qweather_lang = LANGUAGE_MAP.get(ha_lang, "en")
                
                res = await api.city_lookup(raw_loc, lang=qweather_lang)
                
                if res.get("code") == "200" and res.get("location"):
                    self._discovered_locations = res["location"]
                    
                    # 如果只有 1 个结果且是经纬度反查，直接进入确认
                    if len(self._discovered_locations) == 1:
                        return await self._async_verify_and_create(self._discovered_locations[0])
                    
                    # 否则进入城市选择界面
                    return await self.async_step_select_location()
                else:
                    errors["base"] = "location_not_found"
            except Exception as err:
                LOGGER.error("无法连接至 API Host %s: %s", user_host, err)
                errors["base"] = "cannot_connect"

        # 回退到相应表单
        step = "jwt_setup" if config_data.get(CONF_USE_TOKEN) else "user"
        return self.async_show_form(step_id=step, data_schema=self._get_schema(config_data), errors=errors)

    async def async_step_select_location(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """让用户从多个搜索结果中确认城市."""
        if user_input is not None:
            location = next(
                loc for loc in self._discovered_locations 
                if loc["id"] == user_input["location_index"]
            )
            return await self._async_verify_and_create(location)

        # 构造易读的选择列表
        options = [
            {
                "value": loc["id"],
                "label": f"{loc['name']} ({loc['adm2']}, {loc['adm1']}, {loc['country']})"
            }
            for loc in self._discovered_locations
        ]

        return self.async_show_form(
            step_id="select_location",
            data_schema=vol.Schema({
                vol.Required("location_index"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.LIST
                    )
                )
            })
        )

    async def _async_verify_and_create(self, location_info: dict[str, Any]) -> FlowResult:
        """实现地理数据标准化，锁定物理 ID 并创建条目."""
        
        # 1. 提取标准化高精度坐标 (Lon,Lat)
        std_lon = round(float(location_info["lon"]), 2)
        std_lat = round(float(location_info["lat"]), 2)
        normalized_coords = f"{std_lon},{std_lat}"
        
        # 2. 更新临时数据
        self._temp_data[CONF_LOCATION_ID] = normalized_coords
        city_title = location_info["name"]

        # 3. 锁定物理唯一 ID
        unique_id = f"qw_{normalized_coords.replace(',', '_')}"
        await self.async_set_unique_id(unique_id)
        
        if self.source == config_entries.SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(self._get_reconfigure_entry(), data=self._temp_data)
        
        self._abort_if_unique_id_configured()

        # 4. 创建集成条目
        return self.async_create_entry(
            title=city_title, 
            data=self._temp_data,
            options={
                CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
                CONF_DAILYSTEPS: "7",
                CONF_HOURLYSTEPS: "24",
                CONF_GIRD: False,
                CONF_CUSTOM_UI: False,
            }
        )

    def _get_schema(self, data: dict) -> vol.Schema:
        """获取带有当前数据的 Schema 用于错误回显."""
        if data.get(CONF_USE_TOKEN):
            return vol.Schema({
                vol.Required(CONF_PROJECT_ID, default=data.get(CONF_PROJECT_ID)): selector.TextSelector(),
                vol.Required(CONF_KEY_ID, default=data.get(CONF_KEY_ID)): selector.TextSelector(),
            })
        return vol.Schema({
            vol.Required(CONF_HOST, default=data.get(CONF_HOST)): selector.TextSelector(),
            vol.Required(CONF_LOCATION_ID, default=data.get(CONF_LOCATION_ID)): selector.TextSelector(),
            vol.Required(CONF_USE_TOKEN, default=data.get(CONF_USE_TOKEN)): selector.BooleanSelector(),
            vol.Optional(CONF_API_KEY, default=data.get(CONF_API_KEY)): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        })

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """重新配置逻辑."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            # 重新配置时也走一遍搜索校验逻辑
            self._temp_data = {**entry.data, **user_input}
            return await self._async_search_location(self._temp_data)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=entry.data.get(CONF_HOST, "")): selector.TextSelector(),
                vol.Required(CONF_API_KEY, default=entry.data.get(CONF_API_KEY, "")): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            })
        )

class QWeatherOptionsFlow(config_entries.OptionsFlow):
    """处理已安装集成的 UI 选项配置."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """选项配置主界面."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_UPDATE_INTERVAL, 
                    default=options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=5, max=1440, step=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_DAILYSTEPS, 
                    default=str(options.get(CONF_DAILYSTEPS, 7))
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["3", "7", "10", "15", "30"],
                        mode=selector.SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Required(
                    CONF_HOURLYSTEPS, 
                    default=str(options.get(CONF_HOURLYSTEPS, 24))
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["24", "72", "168"],
                        mode=selector.SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Required(CONF_GIRD, default=options.get(CONF_GIRD, False)): selector.BooleanSelector(),
                vol.Required(CONF_CUSTOM_UI, default=options.get(CONF_CUSTOM_UI, False)): selector.BooleanSelector(),
            }),
        )