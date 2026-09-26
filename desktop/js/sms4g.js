
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
    _cmd = { configuration: {} };
  }
  if (!isset(_cmd.type) || !isset(_cmd.subType)) {
    // user is adding a new action message command
    _cmd.type = 'action';
    _cmd.subType = 'message';
  }

  var showUserPhone = _cmd.type == 'action' && _cmd.logicalId != 'send_to_custom_number';

  var rowHtml = '<td>';
  rowHtml += '<input class="cmdAttr form-control input-sm" data-l1key="id" style="display : none;">';
  rowHtml += '<div class="input-group">';
  rowHtml += '<input class="cmdAttr form-control input-sm roundedLeft" data-l1key="name" placeholder="{{Nom de la commande}}">';
  rowHtml += '<span class="input-group-btn"><a class="cmdAction btn btn-sm btn-default" data-l1key="chooseIcon" title="{{Choisir une icône}}"><i class="fas fa-icons"></i></a></span>';
  rowHtml += '<span class="cmdAttr input-group-addon roundedRight" data-l1key="display" data-l2key="icon" style="font-size:19px;padding:0 5px 0 0!important;"></span>';
  rowHtml += '</div>';
  rowHtml += '</td>';
  if (showUserPhone) {
    rowHtml += '<td>';
    rowHtml += '<select class="form-control cmdAttr input-sm" data-l1key="configuration" data-l2key="user"></select>';
    rowHtml += '</td>';
    rowHtml += '<td><input class="cmdAttr form-control input-sm" data-l1key="configuration" data-l2key="phonenumber"></td>';
  } else {
    rowHtml += '<td>';
    rowHtml += '</td>';
    rowHtml += '<td></td>';
  }
  rowHtml += '<td>';
  rowHtml += '<label class="checkbox-inline"><input type="checkbox" class="cmdAttr" data-l1key="isVisible" checked/>{{Afficher}}</label> ';
  rowHtml += '<label class="checkbox-inline"><input type="checkbox" class="cmdAttr" data-l1key="isHistorized" checked/>{{Historiser}}</label> ';
  rowHtml += '</td>';
  rowHtml += '<td>';
  rowHtml += '<span class="cmdAttr" data-l1key="htmlstate"></span>';
  rowHtml += '</td>';
  rowHtml += '<td>';
  rowHtml += '<input class="cmdAttr form-control input-sm" data-l1key="type" value="' + init(_cmd.type) + '" style="display : none;" />';
  rowHtml += '<input class="cmdAttr form-control input-sm" data-l1key="subType" value="' + init(_cmd.subType) + '" style="display : none;" />';
  if (is_numeric(_cmd.id)) {
    rowHtml += '<a class="btn btn-default btn-xs cmdAction" data-action="configure"><i class="fas fa-cogs"></i></a> ';
    rowHtml += '<a class="btn btn-default btn-xs cmdAction" data-action="test"><i class="fas fa-rss"></i> {{Tester}}</a>';
  }
  rowHtml += '<i class="fas fa-minus-circle pull-right cmdAction cursor" data-action="remove" title="{{Supprimer la commande}}"></i></td>';

  var newRow = document.createElement('tr');
  newRow.className = 'cmd';
  newRow.setAttribute('data-cmd_id', init(_cmd.id));
  newRow.innerHTML = rowHtml;

  var tableBody = document.querySelector('#table_cmd tbody');
  if (!tableBody) {
    return;
  }
  tableBody.appendChild(newRow);

  newRow.setJeeValues(_cmd, '.cmdAttr');
  jeedom.cmd.changeType(newRow, init(_cmd.subType));
  jeedom.user.all({
    error: function (error) {
      jeedomUtils.showAlert({ message: error.message, level: 'danger' });
    },
    success: function (data) {
      var option = '<option value="">Aucun</option>';
      for (var i in data) {
        option += '<option value="' + data[i].id + '">' + data[i].login + '</option>';
      }
      var userSelect = newRow.querySelector('.cmdAttr[data-l1key=configuration][data-l2key=user]');
      if (userSelect) {
        userSelect.innerHTML = option;
        userSelect.jeeValue(init(_cmd.configuration.user));
      }
      modifyWithoutSave = false;
    }
  });
}
window.addCmdToTable = addCmdToTable;

var allowUnknownOriginInput = document.querySelector('.eqLogicAttr[data-l2key="allowUnknownOrigin"]');
if (allowUnknownOriginInput) {
  var toggleAutoAddNewNumber = function () {
    var el = document.getElementById('autoAddNewNumber');
    if (el) {
      el.style.display = allowUnknownOriginInput.checked ? 'block' : 'none';
    }
  };
  allowUnknownOriginInput.addEventListener('change', toggleAutoAddNewNumber);
  toggleAutoAddNewNumber();
}

var autoAddNewNumberInput = document.querySelector('.eqLogicAttr[data-l2key="autoAddNewNumber"]');
if (autoAddNewNumberInput) {
  var toggleAutoAddNewNumberWarning = function () {
    var el = document.getElementById('autoAddNewNumberWarning');
    if (el) {
      el.style.display = autoAddNewNumberInput.checked ? 'block' : 'none';
    }
  };
  autoAddNewNumberInput.addEventListener('change', toggleAutoAddNewNumberWarning);
  toggleAutoAddNewNumberWarning();
}

// Ouverture des liens Documentation/Communauté (boutons du bloc "Gestion")
document.body.addEventListener('click', function (event) {
  var locationTarget = event.target.closest('.pluginAction[data-action=openLocation]');
  if (locationTarget) {
    var location = locationTarget.getAttribute('data-location');
    if (location) {
      window.open(location, '_blank', null);
    }
  }
});
})()