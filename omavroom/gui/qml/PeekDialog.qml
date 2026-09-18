// Click-to-peek: show the endpoint and offer a viewer ONLY on explicit click.
// The app never opens a window on its own; closing returns to the wall.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "peekDialog"
    property int seatId: -1
    property string endpoint: ""
    modal: true
    title: "Peek"
    width: 560
    anchors.centerIn: parent

    function openFor(id, ep) {
        seatId = id;
        endpoint = ep;
        open();
    }

    contentItem: ColumnLayout {
        spacing: 10
        Label {
            text: "Seat #" + dialog.seatId + " viewer endpoint"
            color: "#e6edf3"
            font.bold: true
        }
        TextField {
            Layout.fillWidth: true
            text: dialog.endpoint
            readOnly: true
            selectByMouse: true
        }
        Label {
            Layout.fillWidth: true
            text: "Nothing is opened automatically. 'Open viewer' attaches a local viewer"
                + " (configured with --viewer or $OMAVROOM_VIEWER) to this endpoint."
            color: "#8b949e"
            wrapMode: Text.WordWrap
        }
        RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            Button {
                text: "Open viewer"
                onClicked: backend.openViewer(dialog.endpoint)
            }
            Button {
                text: "Close"
                onClicked: dialog.close()
            }
        }
    }
}
