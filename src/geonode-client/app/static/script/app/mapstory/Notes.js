Ext.ns('mapstory.plugins');

mapstory.plugins.NotesManager = Ext.extend(gxp.plugins.Tool, {
    ptype: 'ms_notes_manager',
    menuText: 'Manage annotations',
    gridTitle: 'Mapstory Annotations',
    isNewMap: null,
    outputAction: 0,

    createStore: function () {
        this.store = new GeoExt.data.FeatureStore({
            fields: [
                {name: 'geometry'},
                {name: 'title', type: 'string'}
            ],
            proxy: new GeoExt.data.ProtocolProxy({
                protocol: new mapstory.protocol.Notes({
                    format: new OpenLayers.Format.GeoJSON(),
                    baseUrl: '/maps/' + this.target.id + '/annotations'
                })
            }),
            autoLoad: true
        });
    },

    init: function (target) {
        this.outputConfig = {width: 350, height: 300};
        mapstory.plugins.NotesManager.superclass.init.apply(this, arguments);
        if (this.target.id >= 0) {
            this.createStore();
        }
    },

    addOutput: function () {
        return mapstory.plugins.NotesManager.superclass.addOutput.call(this, {
            xtype: 'gxp_featuregrid',
            title: this.gridTitle,
            store: this.store,
            map:  this.target.mapPanel.map,
            height: 300,
            width: 350
        });
    },

    addActions: function () {
        return mapstory.plugins.NotesManager.superclass.addActions.apply(
            this, [{disabled: !(this.target.id >= 0), text: this.menuText}]);
    }

});

Ext.preg(mapstory.plugins.NotesManager.prototype.ptype, mapstory.plugins.NotesManager);
