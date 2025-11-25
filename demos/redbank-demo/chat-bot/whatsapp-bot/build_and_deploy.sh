#!/bin/bash

# Copyright 2025 IBM, Red Hat
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

SCRIPT_FOLDER="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

exec 3>&1

function _out() {
  echo "$(date +'%F %H:%M:%S') $@"
}

function setup() {
  _out Deploying redbank-whatsapp-bot

  oc new-project redbank-demo 2>/dev/null || oc project redbank-demo
  oc project redbank-demo

  cd ${SCRIPT_FOLDER}

  _out Building WhatsApp Bot image
  oc new-build --name build-redbank-whatsapp-bot --binary --strategy docker --to=image-registry.openshift-image-registry.svc:5000/redbank-demo/redbank-whatsapp-bot:latest
  oc start-build build-redbank-whatsapp-bot --from-dir=. --follow

  _out Done deploying redbank-whatsapp-bot
}

setup
