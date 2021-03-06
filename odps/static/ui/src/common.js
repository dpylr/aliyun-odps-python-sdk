/*
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */
/*
 * Common PyODPS Javascript Module
 */

require(['jupyter-js-widgets'], function (_) {}, function () {
    require.config({
        paths: {
            'jupyter-js-widgets': 'nbextensions/widgets/widgets/js/widget'
        }
    })
});

var pyodps_init_time = new Date();

define('pyodps/common', ['jquery', 'base/js/namespace', 'jupyter-js-widgets'], function ($, IPython) {
    "use strict";

    var view_prompts = {};

    var register_css = function (url) {
        var nbext_path = require.toUrl('nbextensions');
        var user_base_path = nbext_path.substr(0, nbext_path.indexOf('nbextensions'));
        require.config({
            paths: {
                pyodps: user_base_path + 'nbextensions/pyodps'
            }
        });

        if ($('style[data-pyodps-styles="' + url + '"]').length > 0) return;
        var url_parts = require.toUrl(url).split('?', 1);
        var css_url = url_parts[0] + '.css';
        if (url_parts.length > 1) css_url += '?' + url_parts[1];
        $('head').append('<link type="text/css" rel="stylesheet" href="' + css_url + '" data-pyodps-styles="' + url + '" />');
    };

    /**
     * Install a hook for a widget on termination of cell execution
     * @param _widget The widget object
     * @param func The function to be hooked
     */
    var call_on_executed = function (_widget, func) {
        var cell_element = $(_widget.$el.closest('.cell'));
        var view_name = _widget.model.get('_view_name');
        if (undefined == view_prompts[view_name]) {
            view_prompts[view_name] = -1;
        }

        var notifier = function () {
            // only display notifications when the cell stops running.
            if (cell_element.hasClass('running'))
                window.setTimeout(notifier, 100);
            else {
                // ensure that notifications for a cell appear only once.
                var prompt_num = cell_element.data('cell').input_prompt_number;
                if (prompt_num !== view_prompts[view_name]) {
                    view_prompts[view_name] = prompt_num;
                    func.apply(_widget);
                }
            }
        };
        window.setTimeout(notifier, 100);
    };
    $([IPython.events]).on('kernel_restarting.Kernel', function () {
        view_prompts = {};
    });

    register_css('nbextensions/pyodps/styles');

    return {
        init_time: pyodps_init_time,
        register_css: register_css,
        call_on_executed: call_on_executed,
    }
});
