// omavroom Command Center — native monitor wall (Phase 8).
//
// The wall is a responsive GridLayout of PERMANENT slots. The slot set comes
// from `backend.slotKeys` (settings-derived) and only changes when the plan
// changes, so VM lifecycle never adds/removes/moves a tile. Each delegate's
// data is `(backend.revision, backend.slotAt(index))`, which re-evaluates in
// place every poll without recreating the delegate.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: root
    objectName: "omavroomRoot"
    visible: true
    width: 1280
    height: 820
    minimumWidth: 760
    minimumHeight: 480
    title: "omavroom Command Center"
    color: "#0b0e13"

    // Test/observation surface (see omavroom/gui/backend.py docstring).
    property int slotCount: wallRepeater.count
    property int queueCount: backend.queueCount
    property int attentionCount: backend.attentionCount
    property bool bannerVisible: !backend.daemonOk
    property string bannerText: backend.daemonMessage

    // The wall area is the whole region left of the sidebar. It is the single
    // source of truth for the layout: `setViewportSize` positions every slot
    // to FIT this area exactly (tiles scale down; nothing scrolls).
    function syncWallSize() {
        if (wallArea.width > 0 && wallArea.height > 0)
            backend.setViewportSize(Math.round(wallArea.width), Math.round(wallArea.height));
    }
    Component.onCompleted: syncWallSize()

    // Non-visual projection of the wall model. Offscreen Qt does not
    // instantiate visual Repeater delegates, so the headless smoke test reads
    // these `slotProbe-<key>` objects. They are also a convenient seam for any
    // future non-visual logic (e.g. "who would get the next free slot").
    Instantiator {
        id: slotProbe
        model: backend.slotKeys
        delegate: QtObject {
            objectName: "slotProbe-" + modelData
            property var d: (backend.revision, backend.slotAt(index)) || ({})
            property string slotKey: d.key || ""
            property string agentLabel: d.agent || "-"
            property bool offState: !d.occupied
            property string terminalText: d.terminal_text || ""
            property string slotState: d.state || "off"
        }
    }

    header: ToolBar {
        id: toolBar
        background: Rectangle { color: "#11151c" }
        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 14
            anchors.rightMargin: 14
            spacing: 14
            Label {
                text: "omavroom"
                color: "#e6edf3"
                font.bold: true
                font.pixelSize: 16
            }
            Label { text: "Command Center"; color: "#6e7681" }
            Item { Layout.fillWidth: true }
            Label { text: backend.poolText; color: "#9aa4b2" }
            Label { text: "free " + backend.freeText; color: "#9aa4b2" }
            Label { text: "headroom " + backend.headroomText; color: "#9aa4b2" }
            Label { text: "admission " + backend.admissionOverride; color: "#9aa4b2" }
            Button {
                text: "Settings"
                onClicked: settingsDialog.open()
            }
        }
    }

    // Daemon-down banner: clear, retryable, and never blocks the rest of the UI.
    Rectangle {
        id: daemonBanner
        objectName: "daemonBanner"
        visible: !backend.daemonOk
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: visible ? 44 : 0
        color: "#5a1f1f"
        z: 10
        RowLayout {
            anchors.fill: parent
            anchors.margins: 8
            spacing: 10
            Label {
                Layout.fillWidth: true
                text: "DAEMON NOT RUNNING — " + backend.daemonMessage
                color: "#ffd7d7"
                elide: Text.ElideRight
            }
            Button {
                text: "Retry"
                onClicked: backend.retryDaemon()
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.topMargin: daemonBanner.height
        spacing: 0

        // Fit-to-window wall: no scrolling. Each tile is positioned/sized by
        // the pure layout in the backend, and animates to its new geometry
        // when the window resizes or a monitor is focused.
        Item {
            id: wallArea
            objectName: "wallArea"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            onWidthChanged: root.syncWallSize()
            onHeightChanged: root.syncWallSize()
            Component.onCompleted: root.syncWallSize()

            Repeater {
                id: wallRepeater
                model: backend.slotKeys
                delegate: SlotTile {
                    property var d: (backend.revision, backend.slotAt(index)) || ({})
                    property var r: (backend.layoutRevision, backend.slotRectAt(index)) || ({})
                    x: r.x || 0
                    y: r.y || 0
                    width: r.width || 0
                    height: r.height || 0
                    focused: r.focused === true
                    slotData: d
                    onFocusRequested: backend.toggleFocus(d.key)
                    onPeekRequested: backend.requestPeek(d.seat_id)
                    Behavior on x { NumberAnimation { duration: 220; easing.type: Easing.InOutQuad } }
                    Behavior on y { NumberAnimation { duration: 220; easing.type: Easing.InOutQuad } }
                    Behavior on width { NumberAnimation { duration: 220; easing.type: Easing.InOutQuad } }
                    Behavior on height { NumberAnimation { duration: 220; easing.type: Easing.InOutQuad } }
                }
            }
        }

        ColumnLayout {
            Layout.preferredWidth: 350
            Layout.fillHeight: true
            spacing: 8
            QueuePanel {
                Layout.fillWidth: true
                Layout.fillHeight: true
            }
            AttentionPanel {
                Layout.fillWidth: true
                Layout.preferredHeight: 280
            }
            Label {
                Layout.fillWidth: true
                Layout.margins: 8
                visible: backend.lastMessage !== ""
                text: backend.lastMessage
                color: "#7d8590"
                wrapMode: Text.Wrap
            }
        }
    }

    SettingsDialog { id: settingsDialog }
    PeekDialog { id: peekDialog }

    Connections {
        target: backend
        function onPeekReady(seatId, endpoint) {
            peekDialog.openFor(seatId, endpoint)
        }
    }
}
