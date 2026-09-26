
/* This file is part of Jeedom.
 *
 * Jeedom is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * Jeedom is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with Jeedom. If not, see <http://www.gnu.org/licenses/>.
 */

// Protection contre les chargements multiples du script (navigation SPA Jeedom, cache...)
(function () {
  'use strict'

function addCmdToTable(_cmd) {
  if (!isset(_cmd)) {
    _cmd = { configuration: {} }
  }
  if (!isset(_cmd.configuration)) {
    _cmd.configuration = {}
  }
  if (!isset(_cmd.type) || !isset(_cmd.subType)) {
    // user is adding a new action message command
    _cmd.type = 'action'
    _cmd.subType = 'message'
  }

  const showUserPhone = _cmd.type == 'action' && _cmd.logicalId != 'send_to_custom_number'
  const testButtons = is_numeric(_cmd.id)
    ? '<a class="btn btn-default btn-xs cmdAction" data-action="configure"><i class="fas fa-cogs"></i></a> <a class="btn btn-default btn-xs cmdAction" data-action="test"><i class="fas fa-rss"></i> {{Tester}}</a>'
    : ''

  const rowHtml = `<td>
      <input class="cmdAttr form-control input-sm" data-l1key="id" style="display : none;">
      <div class="input-group">
        <input class="cmdAttr form-control input-sm roundedLeft" data-l1key="name" placeholder="{{Nom de la commande}}">
        <span class="input-group-btn"><a class="cmdAction btn btn-sm btn-default" data-l1key="chooseIcon" title="{{Choisir une icône}}"><i class="fas fa-icons"></i></a></span>
        <span class="cmdAttr input-group-addon roundedRight" data-l1key="display" data-l2key="icon" style="font-size:19px;padding:0 5px 0 0!important;"></span>
      </div>
    </td>
    ${showUserPhone
      ? '<td><select class="form-control cmdAttr input-sm" data-l1key="configuration" data-l2key="user"></select></td><td><input class="cmdAttr form-control input-sm" data-l1key="configuration" data-l2key="phonenumber"></td>'
      : '<td></td><td></td>'}
    <td>
      <label class="checkbox-inline"><input type="checkbox" class="cmdAttr" data-l1key="isVisible" checked/>{{Afficher}}</label>
      <label class="checkbox-inline"><input type="checkbox" class="cmdAttr" data-l1key="isHistorized" checked/>{{Historiser}}</label>
    </td>
    <td><span class="cmdAttr" data-l1key="htmlstate"></span></td>
    <td>
      <input class="cmdAttr form-control input-sm" data-l1key="type" value="${init(_cmd.type)}" style="display : none;" />
      <input class="cmdAttr form-control input-sm" data-l1key="subType" value="${init(_cmd.subType)}" style="display : none;" />
      ${testButtons}
      <i class="fas fa-minus-circle pull-right cmdAction cursor" data-action="remove" title="{{Supprimer la commande}}"></i>
    </td>`

  const newRow = Object.assign(document.createElement('tr'), {
    className: 'cmd',
    innerHTML: rowHtml
  })
  newRow.setAttribute('data-cmd_id', init(_cmd.id))

  const tableBody = document.querySelector('#table_cmd tbody')
  if (!tableBody) {
    return console.error('Table body not found')
  }
  tableBody.appendChild(newRow)

  newRow.setJeeValues(_cmd, '.cmdAttr')
  jeedom.cmd.changeType(newRow, init(_cmd.subType))
  jeedom.user.all({
    error: (error) => {
      jeedomUtils.showAlert({ message: error.message, level: 'danger' })
    },
    success: (data) => {
      let option = '<option value="">Aucun</option>'
      for (const i in data) {
        option += '<option value="' + data[i].id + '">' + data[i].login + '</option>'
      }
      const userSelect = newRow.querySelector('.cmdAttr[data-l1key=configuration][data-l2key=user]')
      if (userSelect) {
        userSelect.innerHTML = option
        userSelect.jeeValue(init(_cmd.configuration.user))
      }
      // jeeFrontEnd.modifyWithoutSave (pas le flag global bare "modifyWithoutSave", fragile en mode strict) : evite
      // le message "modifications non enregistrees" apres ce simple peuplement de select, cf utils.js ligne ~766
      jeeFrontEnd.modifyWithoutSave = false
    }
  })
}
window.addCmdToTable = addCmdToTable

const allowUnknownOriginInput = document.querySelector('.eqLogicAttr[data-l2key="allowUnknownOrigin"]')
if (allowUnknownOriginInput) {
  const toggleAutoAddNewNumber = () => {
    const el = document.getElementById('autoAddNewNumber')
    if (el) {
      el.style.display = allowUnknownOriginInput.checked ? 'block' : 'none'
    }
  }
  allowUnknownOriginInput.addEventListener('change', toggleAutoAddNewNumber)
  toggleAutoAddNewNumber()
}

const autoAddNewNumberInput = document.querySelector('.eqLogicAttr[data-l2key="autoAddNewNumber"]')
if (autoAddNewNumberInput) {
  const toggleAutoAddNewNumberWarning = () => {
    const el = document.getElementById('autoAddNewNumberWarning')
    if (el) {
      el.style.display = autoAddNewNumberInput.checked ? 'block' : 'none'
    }
  }
  autoAddNewNumberInput.addEventListener('change', toggleAutoAddNewNumberWarning)
  toggleAutoAddNewNumberWarning()
}

// Ouverture des liens Documentation/Communauté (boutons du bloc "Gestion")
// registerEvent()/unRegisterEvent() (Core Jeedom, dom.utils.js) : nettoyes automatiquement par
// domUtils.unRegisterEvents() a chaque jeedomUtils.loadPage() (navigation SPA), y compris sur
// document.body - pas besoin de garde manuel, l'id nomme evite de toucher aux autres ecouteurs 'click'
// Note : function nommee obligatoire ici (pas de fleche) - unRegisterEvent('click', 'sms4gOpenLocation')
// s'appuie sur listener.name, qu'une arrow function passee inline n'a jamais (contrairement a une const assignee)
document.body.unRegisterEvent('click', 'sms4gOpenLocation').registerEvent('click', function sms4gOpenLocation(event) {
  const locationTarget = event.target.closest('.pluginAction[data-action=openLocation]')
  if (locationTarget) {
    const location = locationTarget.getAttribute('data-location')
    if (location) {
      window.open(location, '_blank', null)
    }
  }
})
})()