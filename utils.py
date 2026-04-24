from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import yaml


@dataclass(frozen=True)
class Config:
	discord_token: str
	debug_mode: str
	spreadsheet_id: str
	pools_tab_id: str

	# Channel IDs
	pool_channel_id: int
	packs_channel_id: int
	second_packs_channel_id: int
	lfm_channel_id: int
	bot_bunker_channel_id: int
	league_committee_channel_id: int
	side_quest_pools_channel_id: int

	bot_name: str

	second_spreadsheet_id: Optional[str] = None
	skip_username: Optional[bool] = None


def get_config(path: Path = Path("config.yaml")) -> Config:
	with open(path) as file:
		config_dict = yaml.load(file, Loader=yaml.FullLoader)
	config = Config(**config_dict)
	return config

