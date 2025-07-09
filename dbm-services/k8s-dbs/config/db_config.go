/*
TencentBlueKing is pleased to support the open source community by making
蓝鲸智云-DB管理系统(BlueKing-BK-DBM) available.

Copyright (C) 2017-2023 THL A29 Limited, a Tencent company. All rights reserved.

Licensed under the MIT License (the "License");
you may not use this file except in compliance with the License.

You may obtain a copy of the License at
https://opensource.org/licenses/MIT

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package config

import "time"

// DatabaseConfig 元数据库配置信息
type DatabaseConfig struct {
	Host         string        `env:"MYSQL_HOST"`
	Port         int           `env:"MYSQL_PORT"`
	User         string        `env:"MYSQL_USER"`
	Password     string        `env:"MYSQL_PASSWORD"`
	DBName       string        `env:"MYSQL_DBNAME"`
	TLSMode      string        `env:"MYSQL_TLSMODE"`
	MaxOpenConns int           `env:"MYSQL_MAX_OPEN_CONN"`
	MaxIdleConns int           `env:"MYSQL_MAX_IDLE_CONN"`
	MaxLifetime  time.Duration `env:"MYSQL_MAX_LIFETIME"`
	MaxIdleTime  time.Duration `env:"MYSQL_MAX_IDLE_TIME"`
}
