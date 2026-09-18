// Settings view: effective config (from local Config), operator-only
// admission override, and the screenshot polling interval.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "settingsDialog"
    modal: true
    title: "Settings"
    width: 680
    height: 660
    anchors.centerIn: parent

    property string pendingAdmission: backend.admissionOverride
    property int pendingInterval: Math.round(backend.pollInterval)

    contentItem: ColumnLayout {
        spacing: 10

        GroupBox {
            Layout.fillWidth: true
            title: "Admission (operator control)"
            RowLayout {
                anchors.fill: parent
                spacing: 10
                Label { text: "Override" }
                ComboBox {
                    id: admissionBox
                    model: ["auto", "allow", "deny"]
                    currentIndex: Math.max(0, model.indexOf(dialog.pendingAdmission))
                    onActivated: dialog.pendingAdmission = currentText
                }
                Label { text: "current: " + backend.admissionOverride; color: "#8b949e" }
                Item { Layout.fillWidth: true }
                Button {
                    text: "Apply"
                    enabled: backend.daemonOk
                    onClicked: backend.setAdmission(dialog.pendingAdmission)
                }
            }
        }

        GroupBox {
            Layout.fillWidth: true
            title: "Screenshot polling"
            RowLayout {
                anchors.fill: parent
                spacing: 10
                Label { text: "Interval (seconds)" }
                SpinBox {
                    id: intervalBox
                    from: 1
                    to: 30
                    value: dialog.pendingInterval
                    onValueModified: dialog.pendingInterval = value
                }
                Label { text: "applies immediately"; color: "#8b949e" }
                Item { Layout.fillWidth: true }
                Button {
                    text: "Apply"
                    onClicked: backend.setPollInterval(dialog.pendingInterval)
                }
            }
        }

        Label { text: "Effective configuration"; color: "#e6edf3"; font.bold: true }
        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            TextArea {
                id: configText
                text: backend.settingsText
                readOnly: true
                wrapMode: TextEdit.NoWrap
                font.family: "monospace"
                font.pixelSize: 12
                selectByMouse: true
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Label {
                Layout.fillWidth: true
                text: "Images are managed in the local config; admission override is sent to the daemon."
                color: "#6e7681"
                wrapMode: Text.Wrap
            }
            Button { text: "Close"; onClicked: dialog.close() }
        }
    }
}
