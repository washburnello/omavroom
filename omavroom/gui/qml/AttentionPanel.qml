// needs_attention panel: held/unrecoverable seats with the operator recovery
// actions (retry-release / force-discard / destroy).
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: panel
    objectName: "attentionPanel"
    property int itemCount: attentionList.count
    color: "#0d1117"
    radius: 8
    border.width: 1
    border.color: "#21262d"

    function ask(action, seatId, seatName) {
        confirmDialog.action = action;
        confirmDialog.seatId = seatId;
        confirmDialog.seatName = seatName;
        confirmDialog.open();
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 10
        spacing: 6

        RowLayout {
            Layout.fillWidth: true
            Label { text: "NEEDS ATTENTION"; color: "#e6edf3"; font.bold: true }
            Item { Layout.fillWidth: true }
            Label { text: backend.attentionCount + ""; color: "#8b949e" }
        }

        Label {
            Layout.fillWidth: true
            visible: backend.attentionCount === 0
            text: "none"
            color: "#4b535d"
        }

        ListView {
            id: attentionList
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            model: backend.attention
            spacing: 6

            delegate: Rectangle {
                id: attentionRow
                width: attentionList.width
                height: 96
                radius: 5
                color: "#1c1214"
                border.width: 1
                border.color: "#f85149"
                property var seatData: modelData

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 8
                    spacing: 4

                    Label {
                        Layout.fillWidth: true
                        text: modelData.name + " (" + modelData.seat_type + ") - " + modelData.state
                        color: "#ffd7d7"
                        font.bold: true
                        elide: Text.ElideRight
                    }
                    Label {
                        Layout.fillWidth: true
                        text: "agent " + modelData.agent + " · project " + modelData.project
                            + (modelData.last_error !== "" ? " · " + modelData.last_error : "")
                        color: "#c9a0a0"
                        elide: Text.ElideRight
                    }
                    RowLayout {
                        spacing: 6
                        Repeater {
                            model: attentionRow.seatData.actions
                            delegate: Button {
                                required property var modelData
                                text: modelData.label
                                onClicked: panel.ask(modelData.id, attentionRow.seatData.seat_id,
                                                     attentionRow.seatData.name)
                            }
                        }
                        Item { Layout.fillWidth: true }
                    }
                }
            }
        }
    }

    Dialog {
        id: confirmDialog
        objectName: "confirmDialog"
        property string action: ""
        property int seatId: -1
        property string seatName: ""
        modal: true
        title: "Confirm recovery action"
        standardButtons: Dialog.Ok | Dialog.Cancel
        anchors.centerIn: parent
        width: 400
        contentItem: Label {
            text: "Run '" + confirmDialog.action + "' on " + confirmDialog.seatName + "?"
            wrapMode: Text.WordWrap
        }
        onAccepted: backend.requestRecovery(confirmDialog.action, confirmDialog.seatId)
    }
}
